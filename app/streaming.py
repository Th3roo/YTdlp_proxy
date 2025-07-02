import io
import os
import re
import asyncio
import logging
from typing import Optional, Dict
import uuid
from fastapi import HTTPException
import yt_dlp
import httpx

TEMP_VIDEO_PARTS_DIR = "temp_video_parts"
os.makedirs(TEMP_VIDEO_PARTS_DIR, exist_ok=True)
logger = logging.getLogger(__name__)


class YTDLPSeekableStream:
    def __init__(
        self,
        url: str,
        ydl_opts: Optional[Dict] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self.url = url
        self.ydl_opts = ydl_opts or {}
        self.ydl_opts.setdefault("noplaylist", True)
        self.loop = loop or asyncio.get_event_loop()

        self.stream_instance_id = str(uuid.uuid4())
        self.instance_temp_dir = os.path.join(
            TEMP_VIDEO_PARTS_DIR, self.stream_instance_id
        )
        os.makedirs(self.instance_temp_dir, exist_ok=True)
        
        self._current_pos = 0
        self._file = None
        self._lock = asyncio.Lock()
        
        # --- Гибридная логика ---
        self.use_direct_httpx = False
        self.stream_url = None
        self.http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

        try:
            # Используем один экземпляр ydl для извлечения информации
            with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
                # process_video_result более надежно выбирает формат на основе нашего запроса
                processed_info = ydl.process_video_result(
                    ydl.extract_info(url, download=False), download=False
                )

            if not processed_info:
                raise HTTPException(status_code=404, detail="Could not process video metadata.")
                
            # Выбираем лучший из запрошенных форматов
            self.format = processed_info.get('requested_formats', [processed_info])[0]
            
            self.total_size = self.format.get("filesize") or self.format.get("filesize_approx")

            # Проверяем, есть ли прямой URL для быстрой загрузки
            if self.format.get("url"):
                self.use_direct_httpx = True
                self.stream_url = self.format.get("url")
                logger.info(f"Найден прямой URL. Используется быстрый режим httpx.")
            else:
                logger.info(f"Прямой URL не найден. Используется надежный режим yt-dlp.")

        except yt_dlp.utils.DownloadError as e:
            self._cleanup_temp_dir()
            raise HTTPException(status_code=502, detail=f"yt-dlp failed to extract video info: {e}") from e
        except Exception as e:
            self._cleanup_temp_dir()
            raise HTTPException(status_code=500, detail=f"Unexpected error extracting video info: {e}") from e

        final_ext = self.format.get("ext", "mp4")
        # Сохраняем шаблон имени файла, т.к. yt-dlp может сам добавлять расширение
        self.filepath_template = os.path.join(self.instance_temp_dir, f"video")
        self.filepath = f"{self.filepath_template}.{final_ext}"

    async def _ensure_downloaded(self, start_byte: int, end_byte: int):
        """Загружает чанк, используя быстрый или надежный метод."""
        # Убедимся, что файл существует, чтобы избежать ошибок с os.path.getsize
        if not os.path.exists(self.filepath):
             # Создаем пустой файл, если его нет
            open(self.filepath, 'a').close()

        current_file_size = os.path.getsize(self.filepath)
        if end_byte < current_file_size:
            # Нужные данные уже есть, ничего не делаем
            return

        # --- Выбор стратегии загрузки ---
        if self.use_direct_httpx:
            await self._ensure_downloaded_httpx(start_byte, end_byte)
        else:
            await self._ensure_downloaded_ytdlp(start_byte, end_byte)

    async def _ensure_downloaded_httpx(self, start_byte: int, end_byte: int):
        """Быстрый метод: загрузка через httpx."""
        headers = {"Range": f"bytes={start_byte}-{end_byte}"}
        try:
            # Используем 'r+b' для записи в существующий файл
            with open(self.filepath, "r+b") as f:
                f.seek(start_byte)
                async with self.http_client.stream("GET", self.stream_url, headers=headers) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        f.write(chunk)
        except Exception as e:
            logger.error(f"httpx download failed: {e}", exc_info=True)
            raise HTTPException(status_code=502, detail=f"Failed to download chunk via httpx: {e}")

    async def _ensure_downloaded_ytdlp(self, start_byte: int, end_byte: int):
        """Надежный метод: загрузка через yt-dlp."""
        dl_opts = self.ydl_opts.copy()
        dl_opts.update({
            'format': self.format['format_id'],
            'outtmpl': f"{self.filepath_template}.%(ext)s",
            'quiet': True,
            'noprogress': True,
            # Важнейшая опция для скачивания части файла
            'http_headers': {'Range': f'bytes={start_byte}-{end_byte}'},
            # Говорим yt-dlp дописывать в файл, а не перезаписывать
            # Это может быть неидеально, но yt-dlp сам управляет фрагментами
            'continuedl': True, 
        })
        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                # Запускаем в экзекуторе, чтобы не блокировать event loop
                await self.loop.run_in_executor(
                    None, ydl.download, [self.url]
                )
        except Exception as e:
            logger.error(f"yt-dlp download failed: {e}", exc_info=True)
            raise HTTPException(status_code=502, detail=f"Failed to download chunk via yt-dlp: {e}")

    async def _open_file(self):
        """Открывает файловый дескриптор, скачивая начальный чанк при необходимости."""
        async with self._lock:
            if self._file is None:
                if not os.path.exists(self.filepath):
                    await self._ensure_downloaded(0, 0)
                try:
                    self._file = await self.loop.run_in_executor(None, open, self.filepath, "rb")
                except FileNotFoundError:
                    raise HTTPException(status_code=500, detail="Stream file not found after initial download.")
            return self._file

    async def read(self, size: int = -1) -> bytes:
        if self.total_size is not None and self._current_pos >= self.total_size:
            return b""
        
        file_handle = await self._open_file()
        if not file_handle: return b""
        
        async with self._lock:
            bytes_to_read = size
            if size == -1:
                bytes_to_read = (self.total_size - self._current_pos) if self.total_size else (1024 * 1024)

            required_end_pos = self._current_pos + bytes_to_read - 1
            
            await self._ensure_downloaded(self._current_pos, required_end_pos)

            await self.loop.run_in_executor(None, file_handle.seek, self._current_pos)
            data = await self.loop.run_in_executor(None, file_handle.read, bytes_to_read)
            self._current_pos += len(data)
            return data

    async def seek(self, offset: int, whence: int = io.SEEK_SET):
        async with self._lock:
            if whence == io.SEEK_SET: new_pos = offset
            elif whence == io.SEEK_CUR: new_pos = self._current_pos + offset
            elif whence == io.SEEK_END:
                if self.total_size is None: raise ValueError("SEEK_END not supported when size is unknown.")
                new_pos = self.total_size + offset
            else: raise ValueError("Invalid whence value.")
            self._current_pos = max(0, new_pos)
            return self._current_pos

    def tell(self) -> int:
        return self._current_pos

    def _cleanup_temp_dir(self):
        if hasattr(self, "instance_temp_dir") and os.path.exists(self.instance_temp_dir):
            try:
                import shutil
                shutil.rmtree(self.instance_temp_dir)
            except Exception as e:
                logger.error(f"Failed to cleanup temp directory {self.instance_temp_dir}: {e}")

    async def close(self):
        async with self._lock:
            if self._file:
                await self.loop.run_in_executor(None, self._file.close)
                self._file = None
            if self.http_client:
                await self.http_client.aclose()
        self._cleanup_temp_dir()


def parse_range_header(range_header: str, total_size: Optional[int]) -> tuple[int, int]:
    # (Эта функция остается без изменений)
    if not range_header or not range_header.lower().startswith("bytes="):
        raise HTTPException(status_code=400, detail="Invalid Range header format.")
    range_spec = range_header.split("=")[1]
    parts = range_spec.split("-")
    try:
        start = int(parts[0]) if parts[0] else 0
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid start byte in Range header.")
    if total_size is not None and start >= total_size:
        raise HTTPException(status_code=416, detail="Range start offset is beyond content length.")
    try:
        end = int(parts[1]) if len(parts) > 1 and parts[1] else total_size - 1
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid end byte in Range header.")
    if total_size is not None:
        end = min(end, total_size - 1)
    if start > end:
        raise HTTPException(status_code=416, detail="Start byte cannot be greater than end byte.")
    return start, end