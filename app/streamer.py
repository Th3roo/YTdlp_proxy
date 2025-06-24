# app/streamer.py

import io
import os
import time
import logging
import yt_dlp
from fastapi import HTTPException

# Настройка базового логгера
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class YTDLSeekableStream:
    """
    Класс, который оборачивает yt-dlp для создания потока с возможностью перемотки.
    Он скачивает части видео по мере необходимости во временный файл.
    """
    def __init__(self, url: str, ydl_opts: dict):
        self.url = url
        self._ydl_opts = ydl_opts
        self._ydl = yt_dlp.YoutubeDL(self._ydl_opts)
        self._current_pos = 0
        self._file = None

        try:
            logging.info(f"Извлечение информации для URL: {self.url}")
            self.info = self._ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            logging.error(f"Не удалось извлечь информацию: {e}")
            raise HTTPException(status_code=404, detail=f"Видео не найдено или недоступно: {e}")

        # Выбираем лучший формат (mp4 предпочтительнее)
        self._format = self._select_best_format()
        if not self._format:
            raise HTTPException(status_code=500, detail="Не найден подходящий формат видео.")
            
        self.total_size = self._format.get('filesize') or self._format.get('filesize_approx')
        if not self.total_size:
            # Если размер неизвестен, перемотка может работать некорректно
            logging.warning("Размер файла неизвестен, полная поддержка перемотки не гарантирована.")
        
        # Создаем временный файл на основе ID видео
        self.filepath = f"{self.info['id']}.ytdlp"

    def _select_best_format(self):
        formats = self.info.get('formats', [self.info])
        best_format = None
        for f in formats:
            # Ищем mp4 с видео и аудио
            if f.get('ext') == 'mp4' and f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                return f
        # Если не нашли, ищем любой формат с видео и аудио
        for f in formats:
            if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                best_format = f
                break
        return best_format

    def _ensure_downloaded(self, start_byte: int, end_byte: int):
        """Гарантирует, что запрошенный диапазон байт загружен."""
        # TODO: В будущем можно реализовать более умную логику,
        # чтобы не скачивать уже имеющиеся части.
        # В текущей реализации мы просто скачиваем нужный сегмент.
        
        # Опции для скачивания конкретного диапазона
        ydl_opts = self._ydl_opts.copy()
        ydl_opts.update({
            "format": self._format['format_id'],
            "outtmpl": self.filepath,
            "http_headers": {"Range": f"bytes={start_byte}-{end_byte}"},
            "overwrites": False, # Не перезаписывать, если файл уже есть
            "continuedl": True
        })

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([self.url])
        except yt_dlp.utils.DownloadError as e:
            logging.error(f"Ошибка скачивания yt-dlp: {e}")
            # Проверяем, существует ли файл, чтобы избежать ошибки открытия
            if not os.path.exists(self.filepath):
                raise HTTPException(status_code=500, detail=f"Ошибка скачивания: {e}")


    def read(self, size: int = -1) -> bytes:
        """Читает `size` байт из потока."""
        if self._file is None:
            # Скачиваем небольшой начальный кусок, если файл еще не открыт
            self._ensure_downloaded(0, 1024 * 1024) # 1MB
            self._file = open(self.filepath, 'rb')

        self._file.seek(self._current_pos)
        data = self._file.read(size)
        self._current_pos += len(data)
        return data

    def seek(self, offset: int, whence: int = io.SEEK_SET):
        """Перемещает курсор в потоке."""
        if whence == io.SEEK_SET:
            self._current_pos = offset
        elif whence == io.SEEK_CUR:
            self._current_pos += offset
        elif whence == io.SEEK_END and self.total_size:
            self._current_pos = self.total_size + offset
        else:
            raise ValueError("Неподдерживаемый 'whence' или размер файла неизвестен.")

        # Убедимся, что запрошенная часть доступна
        if self.total_size:
            self._ensure_downloaded(self._current_pos, min(self._current_pos + 1024 * 1024, self.total_size - 1))
        
        return self._current_pos

    def close(self):
        """Закрывает файловый дескриптор и удаляет временный файл."""
        if self._file:
            self._file.close()
            self._file = None
        if os.path.exists(self.filepath):
            os.remove(self.filepath)
            logging.info(f"Временный файл {self.filepath} удалён.")