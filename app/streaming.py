import io
import os
import re
import asyncio
from typing import Optional, Dict
import uuid
from fastapi import HTTPException
import yt_dlp

TEMP_VIDEO_PARTS_DIR = "temp_video_parts"
os.makedirs(TEMP_VIDEO_PARTS_DIR, exist_ok=True)


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
        self.ydl_opts.setdefault("quiet", True)
        self.ydl_opts.setdefault("noprogress", True)
        self.ydl_opts["format"] = "best"

        self.ydl = yt_dlp.YoutubeDL(self.ydl_opts)
        self.loop = loop or asyncio.get_event_loop()

        self.stream_instance_id = str(uuid.uuid4())
        self.instance_temp_dir = os.path.join(
            TEMP_VIDEO_PARTS_DIR, self.stream_instance_id
        )
        os.makedirs(self.instance_temp_dir, exist_ok=True)

        self.base_filename_in_dir = "video_part"
        self.filepath = os.path.join(
            self.instance_temp_dir, self.base_filename_in_dir + ".ytdlp"
        )

        self._current_pos = 0
        self._file = None

        try:
            self.info_dict = self.ydl.extract_info(url, download=False)
            if not self.info_dict:
                self._cleanup_temp_dir()
                raise HTTPException(
                    status_code=404,
                    detail="Video metadata not found (yt-dlp info_dict is None)",
                )
        except yt_dlp.utils.DownloadError as e:
            self._cleanup_temp_dir()
            if "Unsupported URL" in str(e):
                raise HTTPException(
                    status_code=400, detail=f"Unsupported URL: {self.url}"
                )
            elif "Video unavailable" in str(e):
                raise HTTPException(status_code=404, detail=f"Video unavailable: {e}")
            else:
                raise HTTPException(
                    status_code=502, detail=f"yt-dlp failed to extract video info: {e}"
                )
        except Exception as e:
            self._cleanup_temp_dir()
            raise HTTPException(
                status_code=500, detail=f"Unexpected error extracting video info: {e}"
            )

        self.format = self._get_selected_format()
        if not self.format:
            self._cleanup_temp_dir()
            raise HTTPException(
                status_code=400, detail="No suitable streamable format found by yt-dlp."
            )

        temp_format_info_for_prepare = self.format.copy()
        temp_format_info_for_prepare["id"] = self.info_dict.get("id", "temp_id")
        temp_format_info_for_prepare["title"] = self.info_dict.get(
            "title", "temp_title"
        )
        if "ext" not in temp_format_info_for_prepare:
            temp_format_info_for_prepare["ext"] = self.format.get(
                "video_ext", self.format.get("audio_ext", "mp4")
            )

        self.download_outtmpl = os.path.join(
            self.instance_temp_dir, self.base_filename_in_dir + ".%(ext)s"
        )
        final_ext = self.format.get("ext", "mp4")
        self.filepath = os.path.join(
            self.instance_temp_dir, self.base_filename_in_dir + f".{final_ext}"
        )

        self.total_size = self.format.get("filesize") or self.format.get(
            "filesize_approx"
        )
        if not self.total_size and self.format.get("fragments"):
            pass

        self._lock = asyncio.Lock()

    def _get_selected_format(self):
        formats = self.info_dict.get("formats", [])
        if not formats:
            return None

        requested_format_selector = self.ydl_opts.get("format")

        if requested_format_selector:
            for fmt in formats:
                if fmt.get("format_id") == requested_format_selector:
                    if fmt.get("protocol") in ("http", "https"):
                        return fmt
        try:
            processed_info = self.ydl.process_video_result(
                self.info_dict.copy(), download=False
            )
            selected_formats_list = processed_info.get("requested_formats")
            if selected_formats_list:
                chosen_fmt = selected_formats_list[0]
                if (
                    chosen_fmt.get("protocol") in ("http", "https")
                    and not chosen_fmt.get("is_fragmented")
                    and chosen_fmt.get("fragment_base_url") is None
                ):
                    return chosen_fmt
                else:
                    pass

            if (
                "format_id" in processed_info
                and processed_info.get("protocol") in ("http", "https")
                and not processed_info.get("is_fragmented")
                and processed_info.get("fragment_base_url") is None
            ):
                return processed_info

            best_fallback = None
            for fmt in sorted(
                formats,
                key=lambda f: (f.get("height", 0), f.get("tbr", 0)),
                reverse=True,
            ):
                if (
                    fmt.get("protocol") in ("http", "https")
                    and not fmt.get("is_fragmented")
                    and fmt.get("fragment_base_url") is None
                    and fmt.get("vcodec") != "none"
                ):
                    best_fallback = fmt
                    break
            if best_fallback:
                return best_fallback
            return None
        except (yt_dlp.utils.YtDlpError, KeyError):
            return None

    async def _ensure_downloaded(
        self, start_byte: int, end_byte: Optional[int]
    ):
        max_retries = 5
        retry_delay = 1
        range_header_val = None
        if end_byte is not None:
            if start_byte <= end_byte:
                range_header_val = f"bytes={start_byte}-{end_byte}"
        try:
            if os.path.exists(self.filepath):
                current_file_size = await self.loop.run_in_executor(
                    None, os.path.getsize, self.filepath
                )
                if end_byte is not None and current_file_size >= end_byte + 1:
                    return
        except FileNotFoundError:
            pass

        for attempt in range(max_retries):
            try:
                dl_opts = self.ydl_opts.copy()
                dl_opts.update(
                    {
                        "format": self.format["format_id"],
                        "outtmpl": self.download_outtmpl,
                        "continuedl": True,
                        "noprogress": True,
                        "quiet": True,
                    }
                )
                if range_header_val:
                    dl_opts["http_headers"] = {"Range": range_header_val}

                with yt_dlp.YoutubeDL(dl_opts) as ydl_segment:
                    await self.loop.run_in_executor(
                        None, ydl_segment.download, [self.url]
                    )
                if not await self.loop.run_in_executor(
                    None, os.path.exists, self.filepath
                ):
                    raise FileNotFoundError(
                        f"Download finished but target file {self.filepath} not found."
                    )
                return
            except yt_dlp.utils.DownloadError as e:
                err_str = str(e).lower()
                if (
                    "http error 416" in err_str
                    or "requested range not satisfiable" in err_str
                ):
                    if await self.loop.run_in_executor(
                        None, os.path.exists, self.filepath
                    ):
                        return
                    if attempt == max_retries - 1:
                        self._cleanup_temp_dir()
                        raise HTTPException(
                            status_code=416,
                            detail=f"Requested Range Not Satisfiable: {e}",
                        )
                transient_errors = [
                    "eof occurred in violation of protocol",
                    "connection reset by peer",
                    "ssl_handshake_error",
                    "urlopen error [errno 110] connection timed out",
                    "read error [errno 104] connection reset by peer",
                ]
                if any(sub in err_str for sub in transient_errors):
                    if attempt == max_retries - 1:
                        self._cleanup_temp_dir()
                        raise HTTPException(
                            status_code=503,
                            detail=f"yt-dlp download error after retries (transient): {e}",
                        )
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    self._cleanup_temp_dir()
                    raise HTTPException(
                        status_code=502,
                        detail=f"yt-dlp download error (non-transient): {e}",
                    ) from e
            except FileNotFoundError as e:
                self._cleanup_temp_dir()
                raise HTTPException(
                    status_code=500, detail=f"Failed to find downloaded file: {e}"
                ) from e
            except Exception as e:
                if attempt == max_retries - 1:
                    self._cleanup_temp_dir()
                    raise HTTPException(
                        status_code=500,
                        detail=f"An unexpected error occurred in _ensure_downloaded after retries: {e}",
                    ) from e
                await asyncio.sleep(retry_delay)
                retry_delay *= 2

    async def _open_file(self):
        async with self._lock:
            if self._file is None:
                initial_chunk_size = 1 * 1024 * 1024
                end_byte_for_initial = initial_chunk_size - 1
                if (
                    self.total_size
                    and self.total_size > 0
                    and self.total_size < initial_chunk_size
                ):
                    end_byte_for_initial = self.total_size - 1
                elif self.total_size == 0:
                    end_byte_for_initial = -1

                if end_byte_for_initial >= 0:
                    await self._ensure_downloaded(0, end_byte_for_initial)
                try:
                    self._file = await self.loop.run_in_executor(
                        None, open, self.filepath, "rb"
                    )
                except FileNotFoundError:
                    if self.total_size == 0 and end_byte_for_initial == -1:
                        try:
                            await self.loop.run_in_executor(
                                None, open, self.filepath, "ab"
                            ).close()
                            self._file = await self.loop.run_in_executor(
                                None, open, self.filepath, "rb"
                            )
                        except Exception as e_create:
                            self._cleanup_temp_dir()
                            raise HTTPException(
                                status_code=500,
                                detail=f"Stream file {self.filepath} could not be opened or created (0-byte case).",
                            ) from e_create
                    else:
                        self._cleanup_temp_dir()
                        raise HTTPException(
                            status_code=500,
                            detail=f"Stream file {self.filepath} not found after download attempt.",
                        )
                except Exception as e_open:
                    self._cleanup_temp_dir()
                    raise HTTPException(
                        status_code=500,
                        detail=f"Could not open stream file {self.filepath}: {e_open}",
                    ) from e_open
            return self._file

    async def read(self, size: int = -1) -> bytes:
        file_handle = await self._open_file()
        if size == 0:
            return b""
        if not file_handle:
            return b""

        async with self._lock:
            current_file_size_on_disk = 0
            try:
                current_file_size_on_disk = await self.loop.run_in_executor(
                    None, os.path.getsize, self.filepath
                )
            except FileNotFoundError:
                return b""

            if size == -1:
                if self.total_size is not None:
                    bytes_to_request_from_file = self.total_size - self._current_pos
                    target_read_end_stream_pos = self.total_size
                else:
                    bytes_to_request_from_file = 10 * 1024 * 1024
                    target_read_end_stream_pos = (
                        self._current_pos + bytes_to_request_from_file
                    )
            else:
                bytes_to_request_from_file = size
                target_read_end_stream_pos = self._current_pos + size

            if (
                self.total_size is not None
                and target_read_end_stream_pos > self.total_size
            ):
                target_read_end_stream_pos = self.total_size
                bytes_to_request_from_file = self.total_size - self._current_pos

            if bytes_to_request_from_file < 0:
                bytes_to_request_from_file = 0
            if bytes_to_request_from_file == 0:
                return b""

            required_disk_byte_idx = target_read_end_stream_pos - 1
            if required_disk_byte_idx >= current_file_size_on_disk:
                if not (
                    self.total_size is not None and self._current_pos >= self.total_size
                ):
                    download_start_byte = current_file_size_on_disk
                    download_end_byte = required_disk_byte_idx
                    if self.total_size is None:
                        download_end_byte = None
                    await self._ensure_downloaded(
                        download_start_byte, download_end_byte
                    )

            await self.loop.run_in_executor(None, file_handle.seek, self._current_pos)
            data = await self.loop.run_in_executor(
                None, file_handle.read, bytes_to_request_from_file
            )
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
                    current_disk_size = 0
                    if os.path.exists(self.filepath):
                        current_disk_size = await self.loop.run_in_executor(
                            None, os.path.getsize, self.filepath
                        )
                    await self._ensure_downloaded(current_disk_size, None)
                    if self.total_size is None:
                        if os.path.exists(self.filepath):
                            self._cleanup_temp_dir()
                            raise ValueError(
                                "SEEK_END is not reliably supported when total file size is unknown and couldn't be determined."
                            )
                    new_pos = self.total_size + offset
                else:
                    new_pos = self.total_size + offset
            else:
                self._cleanup_temp_dir()
                raise ValueError(
                    "Invalid whence value. Use io.SEEK_SET, io.SEEK_CUR, or io.SEEK_END."
                )
            if new_pos < 0:
                new_pos = 0
            self._current_pos = new_pos
            return self._current_pos

    def tell(self) -> int:
        return self._current_pos

    def _cleanup_temp_dir(self):
        if hasattr(self, "instance_temp_dir") and os.path.exists(
            self.instance_temp_dir
        ):
            try:
                import shutil
                shutil.rmtree(self.instance_temp_dir)
            except Exception:
                pass

    async def close(self):
        async with self._lock:
            if self._file:
                try:
                    await self.loop.run_in_executor(None, self._file.close)
                except Exception:
                    pass
                finally:
                    self._file = None
            if hasattr(self, "instance_temp_dir") and await self.loop.run_in_executor(
                None, os.path.exists, self.instance_temp_dir
            ):
                try:
                    import shutil
                    await self.loop.run_in_executor(
                        None, shutil.rmtree, self.instance_temp_dir
                    )
                except Exception:
                    pass


def parse_range_header(
    range_header: str, total_size: Optional[int]
) -> tuple[int, int]:
    """
    Parses a Range header string (e.g., "bytes=0-1023") into start and *inclusive* end bytes.
    Raises HTTPException(416) if the range is invalid or unsatisfiable.
    """
    if not range_header or not range_header.lower().startswith("bytes="):
        raise HTTPException(
            status_code=400,
            detail="Invalid Range header format: Must start with 'bytes='",
        )

    range_spec = range_header.split("=")[1]
    parts = range_spec.split("-")
    start_str = parts[0]
    end_str = parts[1] if len(parts) > 1 and parts[1] else None

    try:
        start = int(start_str)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid start byte in Range header"
        )

    if start < 0:
        raise HTTPException(
            status_code=416, detail="Start byte in Range header cannot be negative."
        )

    if total_size is not None:
        if start >= total_size:
            raise HTTPException(
                status_code=416,
                detail=f"Range start offset {start} is beyond content length {total_size}.",
            )

    if end_str:
        try:
            end_inclusive = int(end_str)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid end byte in Range header"
            )
        if end_inclusive < start:
            raise HTTPException(
                status_code=416,
                detail="End byte in Range header cannot be less than start byte.",
            )
        if total_size is not None:
            end_inclusive = min(end_inclusive, total_size - 1)
            if end_inclusive < start and total_size > 0:
                raise HTTPException(
                    status_code=416,
                    detail="Range invalid after adjusting to content length (end < start).",
                )
    else:
        if total_size is None:
            raise HTTPException(
                status_code=400,
                detail="Range 'bytes=N-' requires a known total file size to determine the end boundary for Content-Range header when total size is not known.",
            )
        end_inclusive = total_size - 1

    if total_size == 0:
        if start == 0:
            end_inclusive = -1
        else:
            pass
    return start, end_inclusive
