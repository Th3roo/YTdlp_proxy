import yt_dlp
import asyncio
import logging
from typing import Dict, Any, Optional
from app.config import YDL_OPTS, STREAM_EXTRACT_OPTS
import os

logger = logging.getLogger(__name__)

async def get_video_stream_urls(url: str) -> Optional[Dict[str, Any]]:
    """
    Asynchronously and robustly gets video information using yt-dlp.

    It prioritizes separate, best-quality video and audio streams. If they are
    not available, it falls back to the best available combined stream.
    This function is designed to be resilient to missing metadata and None values.
    """
    loop = asyncio.get_event_loop()
    opts = {
        'quiet': True,
        'noprogress': True,
        'noplaylist': True,
        'no_cookies_from_browser': True,
        # Запрашиваем ВСЕ форматы, чтобы наша логика могла сделать лучший выбор
        'format': 'all',
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info_dict = await loop.run_in_executor(
                None, lambda: ydl.extract_info(url, download=False)
            )

        if not info_dict:
            logger.error(f"yt-dlp returned no info_dict for {url}")
            return None

        all_formats = info_dict.get('formats', [])
        video_url = None
        audio_url = None

        # --- ПОЛНОСТЬЮ НОВАЯ, НАДЕЖНАЯ ЛОГИКА ---

        # 1. Попытка найти лучшие РАЗДЕЛЬНЫЕ потоки
        
        # Фильтруем и сортируем видеопотоки
        # Ключ сортировки `f.get('height') or 0` безопасен и никогда не вернет None
        video_streams = sorted(
            [f for f in all_formats if f.get('vcodec') != 'none' and f.get('acodec') == 'none' and f.get('url')],
            key=lambda f: (f.get('height') or 0, f.get('fps') or 0, f.get('tbr') or 0),
            reverse=True
        )

        # Фильтруем и сортируем аудиопотоки
        # Ключ сортировки `f.get('tbr') or 0` также безопасен
        audio_streams = sorted(
            [f for f in all_formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none' and f.get('url')],
            key=lambda f: (f.get('tbr') or 0, f.get('asr') or 0),
            reverse=True
        )

        if video_streams and audio_streams:
            logger.info(f"Found separate video and audio streams for {url}. Using best of each.")
            video_url = video_streams[0].get('url')
            audio_url = audio_streams[0].get('url')
        
        # 2. Запасной вариант: если раздельных потоков нет, ищем лучший ОБЪЕДИНЕННЫЙ поток
        else:
            logger.warning(f"Could not find separate streams for {url}. Falling back to best combined stream.")
            combined_streams = sorted(
                [f for f in all_formats if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('url')],
                key=lambda f: (f.get('height') or 0, f.get('tbr') or 0),
                reverse=True
            )
            if combined_streams:
                logger.info(f"Found a combined stream for {url}.")
                # Для объединенного потока URL видео и аудио одинаковы
                video_url = combined_streams[0].get('url')
                audio_url = video_url
        
        # 3. Если ничего не найдено, выходим с ошибкой
        if not video_url or not audio_url:
            logger.error(f"Failed to find any usable video/audio URL for {url}")
            return None

        return {
            "id": info_dict.get("id"),
            "title": info_dict.get("title", "Unknown Title"),
            "duration": info_dict.get("duration"),
            "thumbnail": info_dict.get("thumbnail"),
            "video_url": video_url,
            "audio_url": audio_url,
        }
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp DownloadError while fetching stream URLs for {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred while fetching stream URLs for {url}: {e}", exc_info=True)
        return None


async def get_video_info(url: str) -> Optional[Dict[str, Any]]:
    """
    Asynchronously gets video information using yt-dlp.
    Returns a dictionary with information or None in case of an error.
    This version is used by the queue manager for metadata and full downloads.
    """
    ydl_opts = YDL_OPTS.copy()
    ydl_opts.update({
        'extract_flat': 'in_playlist',
        'skip_download': True,
        'forcejson': True,
    })

    loop = asyncio.get_event_loop()

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = await loop.run_in_executor(
                None, lambda: ydl.extract_info(url, download=False)
            )

        if "entries" in info_dict and info_dict["entries"]:
            video_data = info_dict["entries"][0]
        else:
            video_data = info_dict

        return {
            "id": video_data.get("id"),
            "title": video_data.get("title", "Unknown Title"),
            "uploader": video_data.get("uploader", "Unknown Uploader"),
            "duration": video_data.get("duration"),
            "thumbnail": video_data.get("thumbnail"),
            "webpage_url": video_data.get("webpage_url", url),
            "original_url": url,
            "formats": video_data.get("formats"),
        }
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp DownloadError while fetching info for {url}: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred with yt-dlp while fetching info for {url}: {e}", exc_info=True)
        return None


async def download_video(
    url: str, output_path: str = "downloads/%(title)s.%(ext)s"
) -> Optional[str]:
    """
    Asynchronously downloads a video using yt-dlp.
    Returns the path to the downloaded file or None in case of an error.
    """
    download_dir = os.path.dirname(output_path.split("%(")[0])
    if download_dir and not os.path.exists(download_dir):
        os.makedirs(download_dir, exist_ok=True)

    ydl_opts = YDL_OPTS.copy()
    ydl_opts.update({
        'outtmpl': output_path,
    })

    loop = asyncio.get_event_loop()

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            error_code = await loop.run_in_executor(None, lambda: ydl.download([url]))
            if error_code == 0:
                extracted_info = await loop.run_in_executor(
                    None, lambda: ydl.extract_info(url, download=False)
                )

                if "entries" in extracted_info and extracted_info["entries"]:
                    entry_info = extracted_info["entries"][0]
                else:
                    entry_info = extracted_info

                filename = ydl.prepare_filename(entry_info)

                if not os.path.exists(filename):
                    logger.warning(
                        f"File '{filename}' not found after supposedly successful download of '{url}'.")
                return filename
            else:
                logger.error(
                    f"yt-dlp download failed for {url} with error code: {error_code}")
                return None
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp DownloadError during download of {url}: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred during yt-dlp download of {url}: {e}", exc_info=True)
        return None