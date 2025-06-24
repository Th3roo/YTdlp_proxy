# app/streamer.py

import io
import os
import time # Not strictly needed now, but good for potential future use
import logging
import yt_dlp
from fastapi import HTTPException

# Настройка базового логгера
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

DOWNLOAD_CHUNK_SIZE = 1024 * 1024  # 1MB

class YTDLSeekableStream:
    """
    Класс, который оборачивает yt-dlp для создания потока с возможностью перемотки.
    Он скачивает части видео по мере необходимости во временный файл.
    """
    def __init__(self, url: str, ydl_opts: dict):
        self.url = url
        self._ydl_opts = ydl_opts
        # Initialize _ydl here if it's only used for extract_info and not stored long-term
        # For repeated downloads, a new instance is created in _ensure_downloaded
        _initial_ydl = yt_dlp.YoutubeDL(self._ydl_opts)
        self._current_pos = 0
        self._file = None

        try:
            logging.info(f"Извлечение информации для URL: {self.url}")
            self.info = _initial_ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            logging.error(f"Не удалось извлечь информацию: {e}")
            raise HTTPException(status_code=404, detail=f"Видео не найдено или недоступно: {e}")

        # Выбираем лучший формат (mp4 предпочтительнее)
        self._format = self._select_best_format()
        if not self._format:
            raise HTTPException(status_code=500, detail="Не найден подходящий формат видео.")
            
        self.total_size = self._format.get('filesize') or self._format.get('filesize_approx')
        if self.total_size is None: # Check specifically for None, as 0 is a valid size
            # Если размер неизвестен, перемотка может работать некорректно
            logging.warning("Размер файла неизвестен, полная поддержка перемотки не гарантирована.")
        elif self.total_size == 0:
            logging.info("Размер файла 0 байт.")
        
        # Создаем временный файл на основе ID видео
        # It's better to create self.filepath only when needed, or ensure it's cleaned up
        # if __init__ fails partway. For now, this is fine.
        self.filepath = f"{self.info['id']}.ytdlp"

    def _select_best_format(self):
        formats = self.info.get('formats', [self.info])
        best_format = None
        # Prioritize mp4 with video and audio
        for f in formats:
            if (f.get('ext') == 'mp4' and
                f.get('vcodec') != 'none' and f.get('acodec') != 'none' and
                f.get('url')): # Ensure URL exists
                return f
        # Fallback to any format with video, audio, and URL
        for f in formats:
            if (f.get('vcodec') != 'none' and f.get('acodec') != 'none' and
                f.get('url')):
                best_format = f
                break
        # Fallback to the self.info itself if it has a URL (for single format results)
        if not best_format and self.info.get('url'):
            return self.info
        return best_format

    def _ensure_downloaded(self, start_byte: int, end_byte: int):
        """Гарантирует, что запрошенный диапазон байт загружен."""
        
        if start_byte > end_byte:
            logging.warning(
                f"Попытка скачать с start_byte ({start_byte}) > end_byte ({end_byte}). Пропуск скачивания."
            )
            # Ensure the file exists so opening it later doesn't fail, even if it's empty.
            if not os.path.exists(self.filepath):
                try:
                    open(self.filepath, 'a').close()
                except IOError as e:
                    logging.error(f"Не удалось создать/открыть временный файл {self.filepath}: {e}")
                    raise HTTPException(status_code=500, detail=f"Ошибка файловой системы: {e}")
            return

        # TODO: Implement smarter logic to avoid re-downloading existing parts.
        # Current implementation just downloads the requested segment.

        ydl_opts_segment = self._ydl_opts.copy()
        ydl_opts_segment.update({
            "format": self._format['format_id'],
            "outtmpl": {'default': self.filepath}, # Ensure outtmpl is a dict for yt-dlp
            "http_headers": {"Range": f"bytes={start_byte}-{end_byte}"},
            # "overwrites": False, # yt-dlp handles this with outtmpl and continuedl
            "noprogress": True,
            "quiet": True,
            "continuedl": True, # Try to continue if possible
            "retries": 3, # Number of retries
            # "nopart": True, # Avoid .part files if they cause issues; default is False
        })

        logging.info(f"Запрос на скачивание диапазона: {start_byte}-{end_byte} для {self.filepath}")
        try:
            with yt_dlp.YoutubeDL(ydl_opts_segment) as ydl:
                ydl.download([self._format['url']]) # Download from format URL directly
        except yt_dlp.utils.DownloadError as e:
            logging.error(f"Ошибка скачивания yt-dlp для диапазона {start_byte}-{end_byte}: {e}")
            # Check if file exists; if not, the download truly failed to produce anything.
            if not os.path.exists(self.filepath):
                # This might be too aggressive if a partial file was expected.
                # However, if the error is critical, raising it is correct.
                raise HTTPException(status_code=502, detail=f"Ошибка при скачивании сегмента: {e}")
            # If file exists, maybe a partial download happened, or it was an ignorable error.
            # The caller will try to read from self.filepath.
        except Exception as e:
            logging.error(f"Неожиданная ошибка в _ensure_downloaded: {e}")
            raise HTTPException(status_code=500, detail=f"Неожиданная ошибка сервера при скачивании: {e}")


    def read(self, size: int = -1) -> bytes:
        """Читает `size` байт из потока."""
        if self._file is None:
            download_start_byte = 0
            # Default end for a chunk, adjusted if total_size is known
            download_end_byte = DOWNLOAD_CHUNK_SIZE - 1

            if self.total_size is not None:
                if self.total_size == 0:
                    logging.info(f"Файл {self.filepath} имеет нулевой размер. Создание пустого файла.")
                    try:
                        open(self.filepath, 'wb').close() # Create empty file
                        self._file = open(self.filepath, 'rb')
                    except IOError as e:
                        logging.error(f"Не удалось создать/открыть временный файл {self.filepath}: {e}")
                        raise HTTPException(status_code=500, detail=f"Ошибка файловой системы: {e}")
                    # For 0-byte files, no need to call _ensure_downloaded
                elif self.total_size > 0:
                    # Adjust end_byte to not exceed total_size
                    download_end_byte = min(download_end_byte, self.total_size - 1)
                    # Ensure end_byte is not less than start_byte (e.g. if total_size is very small)
                    download_end_byte = max(download_start_byte, download_end_byte)
                    self._ensure_downloaded(download_start_byte, download_end_byte)
                    try:
                        self._file = open(self.filepath, 'rb')
                    except FileNotFoundError:
                        logging.error(f"Временный файл {self.filepath} не найден после попытки начальной загрузки.")
                        raise HTTPException(status_code=500, detail="Ошибка: временный файл не найден.")
                    except IOError as e:
                        logging.error(f"Не удалось открыть временный файл {self.filepath}: {e}")
                        raise HTTPException(status_code=500, detail=f"Ошибка файловой системы при открытии файла: {e}")

            else: # self.total_size is None (unknown)
                self._ensure_downloaded(download_start_byte, download_end_byte)
                try:
                    self._file = open(self.filepath, 'rb')
                except FileNotFoundError:
                    logging.error(f"Временный файл {self.filepath} не найден после попытки начальной загрузки (размер неизвестен).")
                    raise HTTPException(status_code=500, detail="Ошибка: временный файл не найден (размер неизвестен).")
                except IOError as e:
                    logging.error(f"Не удалось открыть временный файл {self.filepath} (размер неизвестен): {e}")
                    raise HTTPException(status_code=500, detail=f"Ошибка файловой системы при открытии файла (размер неизвестен): {e}")

        if not self._file: # Should have been set above or error raised
             logging.error("self._file не установлен в read(), это не должно было произойти.")
             raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера: состояние потока некорректно.")

        self._file.seek(self._current_pos)
        data = self._file.read(size)
        self._current_pos += len(data)
        return data

    def seek(self, offset: int, whence: int = io.SEEK_SET):
        """Перемещает курсор в потоке."""
        if self._file is None:
            # This implies seek is called before any read. Initialize the file.
            # This will perform an initial download if necessary.
            self.read(0)
            # After read(0), self._file is open and current_pos is 0.
            # We need to reset current_pos after this, so seek logic below works from correct base.
            self._current_pos = 0


        if whence == io.SEEK_SET:
            self._current_pos = offset
        elif whence == io.SEEK_CUR:
            self._current_pos += offset
        elif whence == io.SEEK_END:
            if self.total_size is not None:
                self._current_pos = self.total_size + offset
            else:
                raise ValueError("Невозможно использовать SEEK_END: размер файла неизвестен.")
        else:
            raise ValueError(f"Неподдерживаемый 'whence' ({whence}).")

        # Ensure current_pos is within bounds if total_size is known
        if self.total_size is not None:
            self._current_pos = max(0, min(self._current_pos, self.total_size))


        # Determine if we need to download more data
        # We need to ensure data from self._current_pos up to self._current_pos + CHUNK_SIZE is available
        if self.total_size == 0: # No download for empty file
            return self._current_pos

        download_start_byte = self._current_pos
        # Calculate desired end byte for the next chunk
        download_end_byte = self._current_pos + DOWNLOAD_CHUNK_SIZE - 1

        if self.total_size is not None: # total_size is known and > 0
            download_end_byte = min(download_end_byte, self.total_size - 1)

        # Ensure end_byte is not less than start_byte
        download_end_byte = max(download_start_byte, download_end_byte)

        # TODO: Add logic here to check if the required range (download_start_byte to download_end_byte)
        # is already covered by previously downloaded segments to avoid redundant downloads.
        # For now, we download if seek implies new data might be needed.
        # Only download if we actually need to read beyond what might be available,
        # or if it's a forward seek into an unknown area.
        # This simple check is not perfect for already downloaded parts.
        if self._file: # File should be open if seek is called after initial read.
             self._ensure_downloaded(download_start_byte, download_end_byte)
        
        return self._current_pos

    def close(self):
        """Закрывает файловый дескриптор и удаляет временный файл."""
        if self._file:
            try:
                self._file.close()
            except IOError as e:
                logging.warning(f"Ошибка при закрытии файла {self.filepath}: {e}")
            self._file = None
        if os.path.exists(self.filepath):
            try:
                os.remove(self.filepath)
                logging.info(f"Временный файл {self.filepath} удалён.")
            except OSError as e:
                # This could be due to file lock or permissions
                logging.error(f"Не удалось удалить временный файл {self.filepath}: {e}")