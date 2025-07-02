import yt_dlp
import asyncio
from typing import Dict, Any, Optional
from app.config import YDL_OPTS # Import consolidated YDL_OPTS
import os

async def get_video_info(url: str) -> Optional[Dict[str, Any]]:
    """
    Асинхронно получает информацию о видео с помощью yt-dlp.
    Возвращает словарь с информацией или None в случае ошибки.
    """
    # Start with base YDL_OPTS and add/override specific options for get_video_info
    ydl_opts = YDL_OPTS.copy()
    ydl_opts.update({
        'extract_flat': 'in_playlist', # Если это элемент плейлиста, получить только базовую инфу
        'skip_download': True,    # Не скачивать видео, только метаданные
        'forcejson': True,        # Принудительно выводить JSON
        # 'format' больше не переопределяется здесь, используется из YDL_OPTS
    })

    loop = asyncio.get_event_loop()

    try:
        # yt-dlp не является нативно асинхронным, поэтому запускаем в исполнителе потоков
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))

        # Упрощаем вывод, берем только нужные поля
        # Иногда info_dict может содержать 'entries', если это был плейлист, несмотря на 'noplaylist'
        # Берем первый элемент, если это так
        if 'entries' in info_dict and info_dict['entries']:
            video_data = info_dict['entries'][0]
        else:
            video_data = info_dict

        return {
            "id": video_data.get("id"),
            "title": video_data.get("title", "Unknown Title"),
            "uploader": video_data.get("uploader", "Unknown Uploader"),
            "duration": video_data.get("duration"),
            "thumbnail": video_data.get("thumbnail"),
            "webpage_url": video_data.get("webpage_url", url),
            "original_url": url, # Сохраняем оригинальный URL
            "formats": video_data.get("formats") # Может понадобиться для выбора качества
        }
    except yt_dlp.utils.DownloadError as e:
        print(f"yt-dlp DownloadError: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred with yt-dlp: {e}")
        return None

async def download_video(url: str, output_path: str = "downloads/%(title)s.%(ext)s") -> Optional[str]:
    """
    Асинхронно скачивает видео с помощью yt-dlp.
    Возвращает путь к скачанному файлу или None в случае ошибки.
    """
    # Убедимся, что директория для скачивания существует
    import os
    download_dir = os.path.dirname(output_path.split('%(')[0]) # Получаем базовую директорию
    if download_dir and not os.path.exists(download_dir):
        os.makedirs(download_dir, exist_ok=True)

    # Start with base YDL_OPTS and add/override specific options for download_video
    ydl_opts = YDL_OPTS.copy()
    ydl_opts.update({
        'outtmpl': output_path, # Шаблон для имени выходного файла
        # 'format' больше не переопределяется здесь, используется из YDL_OPTS
        # 'progress_hooks': [my_hook], # Можно добавить хуки для отслеживания прогресса
    })

    loop = asyncio.get_event_loop()

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Запускаем скачивание в исполнителе потоков
            error_code = await loop.run_in_executor(None, lambda: ydl.download([url]))
            if error_code == 0:
                # yt-dlp сам формирует имя файла на основе шаблона,
                # но его нужно "угадать" или получить из хуков.
                # Пока что просто вернем предполагаемый путь, если нет ошибок.
                # Для более точного определения имени файла может потребоваться более сложная логика
                # или использование хуков yt-dlp для получения информации о файле после скачивания.
                # Простой способ - найти последний измененный файл в директории, но это не очень надежно.
                # Предположим, что yt-dlp успешно создал файл по шаблону.
                # Если outtmpl имеет плейсхолдеры, точное имя файла может отличаться.
                # Мы вернем шаблон, пользователь API должен будет его обработать или мы должны улучшить это.
                # Для простоты пока возвращаем "успех" и предполагаемый шаблон.
                # TODO: Улучшить определение фактического имени файла. (Это сделано ниже)

                # После успешного скачивания (error_code == 0), файл должен существовать по пути,
                # который yt-dlp определил бы с помощью prepare_filename с теми же опциями.
                # Мы вызываем extract_info еще раз (без скачивания), чтобы получить доступ
                # к информации, которую ydl использует для формирования имени файла.
                # Это должно быть консистентно с тем, как yt-dlp назвал файл при скачивании.
                # Важно, чтобы 'outtmpl' в ydl_opts был тем же, что и при скачивании.

                # Запускаем extract_info в исполнителе, чтобы не блокировать основной поток
                extracted_info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))

                # Если 'entries' есть, это плейлист, берем первое видео (хотя noplaylist=True должно это предотвращать)
                # Это для случая, если URL сам по себе является одиночным видео из плейлиста, но yt-dlp все равно его так обрабатывает.
                if 'entries' in extracted_info and extracted_info['entries']:
                     entry_info = extracted_info['entries'][0]
                else:
                     entry_info = extracted_info

                # Получаем имя файла, которое yt-dlp сгенерировал бы (и должен был сгенерировать)
                filename = ydl.prepare_filename(entry_info)

                # Дополнительная проверка на существование файла
                if not os.path.exists(filename):
                    print(f"Warning: File '{filename}' not found after supposedly successful download of '{url}'. "
                          f"This might happen if outtmpl in ydl_opts for download and prepare_filename differ, "
                          f"or if special characters in title/id caused unexpected naming.")
                    # В этом случае можно вернуть None или шаблон, но лучше разобраться в причине.
                    # Пока что вернем предсказанное имя, но с предупреждением.
                return filename
            else:
                print(f"yt-dlp download failed with error code: {error_code} for url: {url}")
                return None
    except yt_dlp.utils.DownloadError as e:
        print(f"yt-dlp DownloadError during download: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred during yt-dlp download: {e}")
        return None

# Пример использования (для тестирования этого модуля)
if __name__ == "__main__":
    async def main():
        # Test get_video_info
        test_url_info = "https://www.youtube.com/watch?v=dQw4w9WgXcQ" # Rick Astley
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

        # Test download_video
        test_url_download = "https://www.youtube.com/watch?v=ស្លጥไทย মিশ্র" # A short Creative Commons video
        # (Using a non-English filename part to test encoding handling by yt-dlp)
        # Note: For very short videos, yt-dlp might be faster than the overhead of asyncio executor.
        # For actual usage, ensure the video URL is valid and downloadable.
        # This is a placeholder URL that might not work.
        # Let's use a known short CC video.
        test_url_download_cc = "https://www.youtube.com/watch?v=y_zSBt0A3dY" # Example: Blender Open Movie "Big Buck Bunny" Trailer (short)

        print(f"Attempting to download: {test_url_download_cc}")
        # Создаем папку downloads, если ее нет
        if not os.path.exists("downloads"):
            os.makedirs("downloads")

        downloaded_file_path = await download_video(test_url_download_cc, output_path="downloads/%(title)s [%(id)s].%(ext)s")
        if downloaded_file_path:
            print(f"Video downloaded successfully to: {downloaded_file_path}")
            # Проверка существования файла
            if os.path.exists(downloaded_file_path):
                print(f"File '{downloaded_file_path}' confirmed to exist.")
            else:
                print(f"File '{downloaded_file_path}' NOT FOUND. yt-dlp might have used a different naming scheme or failed silently post-download.")
        else:
            print("Failed to download video.")

    asyncio.run(main())