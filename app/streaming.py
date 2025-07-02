import io
import os
import re
import asyncio
import logging
from typing import Optional, Dict
import uuid
from fastapi import HTTPException
import yt_dlp
import httpx # Используем httpx для прямых запросов

TEMP_VIDEO_PARTS_DIR = "temp_video_parts"
os.makedirs(TEMP_VIDEO_PARTS_DIR, exist_ok=True)
logger = logging.getLogger(__name__)


def get_safe_filename(name: str) -> str:
    """Sanitize a string to be used as a filename."""
    name = re.sub(r"[^\w\s-]", "", name).strip()
    name = re.sub(r"[-\s]+", "-", name)
    return name if len(name) <= 200 else name[:200]


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
        
        # httpx клиент для загрузки чанков
        self.http_client = httpx.AsyncClient(timeout=30.0)

        try:
            with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
                self.info_dict = ydl.extract_info(url, download=False)
            if not self.info_dict:
                raise HTTPException(
                    status_code=404, detail="Video metadata not found (yt-dlp info_dict is None)"
                )
        except yt_dlp.utils.DownloadError as e:
            self._cleanup_temp_dir()
            if "Unsupported URL" in str(e):
                raise HTTPException(status_code=400, detail=f"Unsupported URL: {self.url}") from e
            if "Video unavailable" in str(e):
                raise HTTPException(status_code=404, detail=f"Video unavailable: {e}") from e
            raise HTTPException(status_code=502, detail=f"yt-dlp failed to extract video info: {e}") from e
        except Exception as e:
            self._cleanup_temp_dir()
            raise HTTPException(status_code=500, detail=f"Unexpected error extracting video info: {e}") from e

        self.format = self._get_selected_format()
        if not self.format or not self.format.get("url"):
            self._cleanup_temp_dir()
            raise HTTPException(
                status_code=400, detail="No suitable streamable format with a direct URL found."
            )
            
        self.stream_url = self.format.get("url")
        self.total_size = self.format.get("filesize") or self.format.get("filesize_approx")

        final_ext = self.format.get("ext", "mp4")
        self.filepath = os.path.join(self.instance_temp_dir, f"video.{final_ext}")


    def _get_selected_format(self):
        """Выбирает лучший нефрагментированный формат с прямым URL."""
        formats = self.info_dict.get("formats", [])
        if not formats:
            return None

        # Ищем лучший формат с видео и аудио, который не является фрагментированным
        best_format = None
        for f in sorted(
            formats,
            key=lambda x: x.get("filesize", 0) or x.get("filesize_approx", 0),
            reverse=True,
        ):
            if (
                f.get("protocol") in ("http", "https")
                and not f.get("is_fragmented")
                and f.get("fragment_base_url") is None
                and f.get("vcodec") != "none"
                and f.get("acodec") != "none"
                and f.get("url")
            ):
                best_format = f
                break
        
        # Если не нашли комбинированный, ищем лучший видео-формат
        if not best_format:
             for f in sorted(
                formats,
                key=lambda x: (x.get("height", 0), x.get("tbr", 0)),
                reverse=True,
            ):
                if (
                    f.get("protocol") in ("http", "https")
                    and not f.get("is_fragmented")
                    and f.get("fragment_base_url") is None
                    and f.get("vcodec") != "none"
                    and f.get("url")
                ):
                    best_format = f
                    break

        return best_format


    async def _ensure_downloaded(self, start_byte: int, end_byte: int):
        """
        Загружает указанный диапазон байтов из self.stream_url в кеш-файл self.filepath.
        Использует httpx для более эффективной загрузки.
        """
        max_retries = 3
        retry_delay = 1

        headers = {"Range": f"bytes={start_byte}-{end_byte}"}

        for attempt in range(max_retries):
            try:
                # Открываем файл в режиме 'r+b' (чтение/запись), создаем если его нет.
                # 'wb' создаст файл, 'r+b' позволит писать в середину.
                mode = "r+b" if os.path.exists(self.filepath) else "wb"
                f = await self.loop.run_in_executor(None, open, self.filepath, mode)
                
                try:
                    await self.loop.run_in_executor(None, f.seek, start_byte)
                    async with self.http_client.stream("GET", self.stream_url, headers=headers) as response:
                        if response.status_code == 416:  # Range Not Satisfiable
                            logger.warning(f"Range {start_byte}-{end_byte} not satisfiable for {self.url}")
                            return
                        response.raise_for_status()
                        
                        async for chunk in response.aiter_bytes():
                            await self.loop.run_in_executor(None, f.write, chunk)
                        return # Успешная загрузка, выходим из цикла
                finally:
                    await self.loop.run_in_executor(None, f.close)

            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                logger.warning(
                    f"Attempt {attempt + 1}/{max_retries} failed for range {start_byte}-{end_byte}. "
                    f"Error: {e}. Retrying in {retry_delay}s..."
                )
                if attempt == max_retries - 1:
                    self._cleanup_temp_dir()
                    raise HTTPException(status_code=502, detail=f"Failed to download video chunk after retries: {e}") from e
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
            except Exception as e:
                 logger.error(f"Unexpected error in _ensure_downloaded: {e}", exc_info=True)
                 self._cleanup_temp_dir()
                 raise HTTPException(status_code=500, detail=f"Unexpected error while downloading chunk: {e}") from e

    async def _open_file(self):
        """Открывает файловый дескриптор, скачивая начальный чанк при необходимости."""
        async with self._lock:
            if self._file is None:
                # Убедимся, что хотя бы первый байт существует, чтобы файл был создан
                if not os.path.exists(self.filepath):
                    await self._ensure_downloaded(0, 0)
                
                try:
                    self._file = await self.loop.run_in_executor(None, open, self.filepath, "rb")
                except FileNotFoundError:
                    self._cleanup_temp_dir()
                    raise HTTPException(status_code=500, detail=f"Stream file {self.filepath} not found after initial download attempt.")
            return self._file

    async def read(self, size: int = -1) -> bytes:
        if not self.total_size and size == -1:
             size = 1024 * 1024 # Читаем по 1МБ, если размер неизвестен
             
        file_handle = await self._open_file()
        if not file_handle:
            return b""
        
        async with self._lock:
            if self.total_size is not None and self._current_pos >= self.total_size:
                return b""

            bytes_to_read = size
            if size == -1:
                bytes_to_read = self.total_size - self._current_pos
            
            # Определяем, какой диапазон байт нам нужен
            required_end_pos = self._current_pos + bytes_to_read - 1

            # Проверяем размер файла на диске
            current_file_size = await self.loop.run_in_executor(None, os.path.getsize, self.filepath)
            
            # Если нужных данных нет на диске, скачиваем их
            if required_end_pos >= current_file_size:
                # Скачиваем недостающий диапазон
                download_start_byte = current_file_size
                download_end_byte = required_end_pos
                if self.total_size is not None:
                     download_end_byte = min(download_end_byte, self.total_size - 1)
                
                if download_start_byte <= download_end_byte:
                    await self._ensure_downloaded(download_start_byte, download_end_byte)

            # Читаем данные из локального файла
            await self.loop.run_in_executor(None, file_handle.seek, self._current_pos)
            data = await self.loop.run_in_executor(None, file_handle.read, bytes_to_read)
            self._current_pos += len(data)
            return data

    async def seek(self, offset: int, whence: int = io.SEEK_SET):
        async with self._lock:
            if whence == io.SEEK_SET:
                new_pos = offset
            elif whence == io.SEEK_CUR:
                new_pos = self._current_pos + offset
            elif whence == io.SEEK_END:
                if self.total_size is None:
                    raise ValueError("SEEK_END is not supported when total file size is unknown.")
                new_pos = self.total_size + offset
            else:
                raise ValueError("Invalid whence value. Use io.SEEK_SET, io.SEEK_CUR, or io.SEEK_END.")

            if new_pos < 0:
                new_pos = 0

            self._current_pos = new_pos
            return self._current_pos

    def tell(self) -> int:
        return self._current_pos

    def _cleanup_temp_dir(self):
        if hasattr(self, "instance_temp_dir") and os.path.exists(self.instance_temp_dir):
            try:
                import shutil
                shutil.rmtree(self.instance_temp_dir)
                logger.info(f"Cleaned up temp directory: {self.instance_temp_dir}")
            except Exception as e:
                logger.error(f"Failed to cleanup temp directory {self.instance_temp_dir}: {e}", exc_info=True)

    async def close(self):
        async with self._lock:
            if self._file:
                await self.loop.run_in_executor(None, self._file.close)
                self._file = None
            if self.http_client:
                await self.http_client.aclose()
        self._cleanup_temp_dir()


def parse_range_header(
    range_header: str, total_size: Optional[int]
) -> tuple[int, int]:
    if not range_header or not range_header.lower().startswith("bytes="):
        raise HTTPException(
            status_code=400, detail="Invalid Range header format: Must start with 'bytes='",
        )

    range_spec = range_header.split("=")[1]
    parts = range_spec.split("-")
    start_str = parts[0]
    end_str = parts[1] if len(parts) > 1 and parts[1] else None

    try:
        start = int(start_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid start byte in Range header") from None

    if total_size is not None and start >= total_size:
        raise HTTPException(
            status_code=416, detail=f"Range start offset {start} is beyond content length {total_size}.",
        )

    if end_str:
        try:
            end_inclusive = int(end_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end byte in Range header") from None
        if total_size is not None:
            end_inclusive = min(end_inclusive, total_size - 1)
    else:
        if total_size is None:
            # Не можем удовлетворить открытый диапазон без знания общего размера
            raise HTTPException(status_code=416, detail="Range requests 'bytes=N-' are not supported without a known total size.")
        end_inclusive = total_size - 1
        
    if start > end_inclusive:
        raise HTTPException(
            status_code=416, detail="Start byte cannot be greater than end byte.",
        )

    return start, end_inclusive