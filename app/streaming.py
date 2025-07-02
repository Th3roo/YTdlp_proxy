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
        self.http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

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
        
        # Используем .get(key) or 0 для безопасной сортировки, даже если ключи отсутствуют или их значение None
        key_func_combined = lambda f: f.get("filesize") or f.get("filesize_approx") or 0
        
        sorted_formats = sorted(formats, key=key_func_combined, reverse=True)

        for f in sorted_formats:
            if (
                f.get("protocol") in ("http", "https")
                and not f.get("is_fragmented")
                and f.get("vcodec") != "none"
                and f.get("acodec") != "none"
                and f.get("url")
            ):
                best_format = f
                break

        # Если не нашли комбинированный, ищем лучший видео-формат
        if not best_format:
            # ИСПРАВЛЕНИЕ: Используем `f.get(key) or 0` для безопасности при сортировке
            key_func_video = lambda f: (f.get("height") or 0, f.get("tbr") or 0)
            
            sorted_formats_video = sorted(formats, key=key_func_video, reverse=True)
            
            for f in sorted_formats_video:
                if (
                    f.get("protocol") in ("http", "https")
                    and not f.get("is_fragmented")
                    and f.get("vcodec") != "none"
                    and f.get("url")
                ):
                    best_format = f
                    break

        return best_format


    async def _ensure_downloaded(self, start_byte: int, end_byte: int):
        """Загружает указанный диапазон байтов из self.stream_url в кеш-файл."""
        max_retries = 3
        retry_delay = 1
        headers = {"Range": f"bytes={start_byte}-{end_byte}"}

        for attempt in range(max_retries):
            try:
                mode = "r+b" if os.path.exists(self.filepath) else "wb"
                with open(self.filepath, mode) as f:
                    f.seek(start_byte)
                    async with self.http_client.stream("GET", self.stream_url, headers=headers) as response:
                        if response.status_code == 416:
                            logger.warning(f"Range {start_byte}-{end_byte} not satisfiable.")
                            return
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)
                        return
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                logger.warning(f"Attempt {attempt + 1} failed for range {start_byte}-{end_byte}: {e}. Retrying...")
                if attempt == max_retries - 1:
                    self._cleanup_temp_dir()
                    raise HTTPException(status_code=502, detail=f"Failed to download chunk: {e}") from e
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
            except Exception as e:
                 logger.error(f"Unexpected error in _ensure_downloaded: {e}", exc_info=True)
                 self._cleanup_temp_dir()
                 raise HTTPException(status_code=500, detail=f"Unexpected error while writing chunk: {e}") from e

    async def _open_file(self):
        """Открывает файловый дескриптор, скачивая начальный чанк при необходимости."""
        async with self._lock:
            if self._file is None:
                if not os.path.exists(self.filepath):
                    await self._ensure_downloaded(0, 0)
                try:
                    self._file = await self.loop.run_in_executor(None, open, self.filepath, "rb")
                except FileNotFoundError:
                    self._cleanup_temp_dir()
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
                bytes_to_read = self.total_size - self._current_pos if self.total_size else 1024 * 1024

            required_end_pos = self._current_pos + bytes_to_read - 1
            current_file_size = os.path.getsize(self.filepath)
            
            if required_end_pos >= current_file_size:
                download_start_byte = current_file_size
                download_end_byte = required_end_pos
                if self.total_size:
                     download_end_byte = min(download_end_byte, self.total_size - 1)
                
                if download_start_byte <= download_end_byte:
                    await self._ensure_downloaded(download_start_byte, download_end_byte)

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
                logger.info(f"Cleaned up temp directory: {self.instance_temp_dir}")
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


def parse_range_header(
    range_header: str, total_size: Optional[int]
) -> tuple[int, int]:
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