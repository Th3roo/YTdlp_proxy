import yt_dlp
import asyncio
import logging
from typing import Dict, Any, Optional
from app.config import YDL_OPTS
import os

logger = logging.getLogger(__name__)

async def get_video_info(url: str) -> Optional[Dict[str, Any]]:
    """
    Asynchronously gets video information using yt-dlp.
    Returns a dictionary with information or None in case of an error.
    """
    ydl_opts = YDL_OPTS.copy()
    ydl_opts.update({
        'extract_flat': 'in_playlist', # Если это элемент плейлиста, получить только базовую инфу
        'skip_download': True,    # Не скачивать видео, только метаданные
        'forcejson': True,        # Принудительно выводить JSON
        # 'format' больше не переопределяется здесь, используется из YDL_OPTS
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
        'outtmpl': output_path, # Шаблон для имени выходного файла
        # 'format' больше не переопределяется здесь, используется из YDL_OPTS
        # 'progress_hooks': [my_hook], # Можно добавить хуки для отслеживания прогресса
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


if __name__ == "__main__":

    async def main():
        test_url_info = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        logger.info(f"Fetching info for: {test_url_info}")
        info = await get_video_info(test_url_info)
        if info:
            logger.info(f"Title: {info.get('title')}")
            logger.info(f"Duration: {info.get('duration')}s")
            logger.info(f"Uploader: {info.get('uploader')}")
            logger.info(f"Thumbnail: {info.get('thumbnail')}")
        else:
            logger.error("Failed to get video info.")

        logger.info("-" * 20)

        test_url_download_cc = "https://www.youtube.com/watch?v=y_zSBt0A3dY"

        logger.info(f"Attempting to download: {test_url_download_cc}")
        if not os.path.exists("downloads"):
            os.makedirs("downloads")

        downloaded_file_path = await download_video(
            test_url_download_cc, output_path="downloads/%(title)s [%(id)s].%(ext)s"
        )
        if downloaded_file_path:
            logger.info(f"Video downloaded successfully to: {downloaded_file_path}")
            if os.path.exists(downloaded_file_path):
                logger.info(f"File '{downloaded_file_path}' confirmed to exist.")
            else:
                logger.warning(f"File '{downloaded_file_path}' NOT FOUND after download.")
        else:
            logger.error("Failed to download video.")

    asyncio.run(main())