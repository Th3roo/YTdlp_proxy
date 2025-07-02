import yt_dlp
import asyncio
from typing import Dict, Any, Optional
from app.config import YDL_OPTS
import os

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
        print(f"yt-dlp DownloadError: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred with yt-dlp: {e}")
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
                    print(
                        f"Warning: File '{filename}' not found after supposedly successful download of '{url}'."
                    )
                return filename
            else:
                print(
                    f"yt-dlp download failed with error code: {error_code} for url: {url}"
                )
                return None
    except yt_dlp.utils.DownloadError as e:
        print(f"yt-dlp DownloadError during download: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred during yt-dlp download: {e}")
        return None


if __name__ == "__main__":

    async def main():
        test_url_info = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        print(f"Fetching info for: {test_url_info}")
        info = await get_video_info(test_url_info)
        if info:
            print(f"Title: {info.get('title')}")
            print(f"Duration: {info.get('duration')}s")
            print(f"Uploader: {info.get('uploader')}")
            print(f"Thumbnail: {info.get('thumbnail')}")
        else:
            print("Failed to get video info.")

        print("-" * 20)

        test_url_download_cc = "https://www.youtube.com/watch?v=y_zSBt0A3dY"

        print(f"Attempting to download: {test_url_download_cc}")
        if not os.path.exists("downloads"):
            os.makedirs("downloads")

        downloaded_file_path = await download_video(
            test_url_download_cc, output_path="downloads/%(title)s [%(id)s].%(ext)s"
        )
        if downloaded_file_path:
            print(f"Video downloaded successfully to: {downloaded_file_path}")
            if os.path.exists(downloaded_file_path):
                print(f"File '{downloaded_file_path}' confirmed to exist.")
            else:
                print(f"File '{downloaded_file_path}' NOT FOUND.")
        else:
            print("Failed to download video.")

    asyncio.run(main())