import io
import os
import re
import time
import uuid
import asyncio
from typing import Annotated, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import StreamingResponse
import yt_dlp

# Директория для временных частей видео
TEMP_VIDEO_PARTS_DIR = "temp_video_parts"
os.makedirs(TEMP_VIDEO_PARTS_DIR, exist_ok=True)

def get_safe_filename(name: str) -> str:
    """ Sanitize a string to be used as a filename. """
    name = re.sub(r'[^\w\s-]', '', name).strip()
    name = re.sub(r'[-\s]+', '-', name)
    return name if len(name) <= 200 else name[:200] # Limit length

class YTDLPSeekableStream:
    def __init__(self, url: str, ydl_opts: dict = None, loop: Optional[asyncio.AbstractEventLoop] = None):
        self.url = url
        self.ydl_opts = ydl_opts or {}
        # Ensure some defaults for streaming
        self.ydl_opts.setdefault('noplaylist', True)
        self.ydl_opts.setdefault('quiet', True)
        self.ydl_opts.setdefault('noprogress', True)

        self.ydl = yt_dlp.YoutubeDL(self.ydl_opts)
        self.loop = loop or asyncio.get_event_loop()

        try:
            self.info_dict = self.ydl.extract_info(url, download=False)
            if not self.info_dict:
                raise HTTPException(status_code=404, detail="Video metadata not found by yt-dlp")
        except yt_dlp.utils.DownloadError as e:
            raise HTTPException(status_code=502, detail=f"yt-dlp failed to extract info: {e}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Unexpected error during info extraction: {e}")

        self.format = self._get_selected_format()
        if not self.format:
            raise HTTPException(status_code=400, detail="No suitable streamable format found by yt-dlp.")

        # Generate a unique filepath for this stream instance
        base_filename = get_safe_filename(self.info_dict.get("title", "untitled_video"))
        unique_id = str(uuid.uuid4())
        # yt-dlp adds its own extension based on format, so we just provide a base path.
        # However, for partial downloads, yt-dlp might create .part files or use specific naming.
        # We'll let yt-dlp manage the exact final name within its outtmpl.
        # The .ytdlp extension was from your original code, let's see if we need it or if yt-dlp's native naming is better.
        # For now, let's use a simpler unique name and let 'outtmpl' in _ensure_downloaded define the full path.
        # We need a reliable way to get the final path, though.
        # Let's stick to a unique directory for each stream for now.
        self.stream_instance_dir = os.path.join(TEMP_VIDEO_PARTS_DIR, unique_id)
        os.makedirs(self.stream_instance_dir, exist_ok=True)

        # The actual file path will be determined by yt-dlp's outtmpl when downloading.
        # We need to predict it or retrieve it after download.
        # For `outtmpl`, we'll use a simple name like 'video' inside the unique directory.
        self.download_outtmpl = os.path.join(self.stream_instance_dir, 'video.%(ext)s')

        # Predict the filepath based on outtmpl and format extension
        # This is tricky as the exact extension might vary.
        # Let's assume the first extension from the format if available.
        fmt_ext = self.format.get('ext', 'mp4') # Default to mp4 if not found
        self.filepath = os.path.join(self.stream_instance_dir, f'video.{fmt_ext}')


        self._current_pos = 0
        self.total_size = self.format.get("filesize") or self.format.get("filesize_approx")
        if not self.total_size and self.format.get('fragments'): # For fragmented streams like HLS/DASH native
            # This class isn't designed for native HLS/DASH (where yt-dlp gives .m3u8)
            # It's for progressive download of single files.
            # If filesize is truly unknown for a progressive stream, it's problematic for Range requests.
            pass # total_size remains None

        self._file = None
        self._lock = asyncio.Lock() # Lock for file operations and download state

    def _get_selected_format(self):
        # Prefer formats that are single files and suitable for streaming
        # Example: format_selector = "best[protocol^=http][ext=mp4]/best[ext=mp4]/best"
        # Your original logic is good for selecting based on format_id or requested_formats
        formats = self.info_dict.get("formats", [])
        if not formats:
            return None

        # If a specific format ID is requested in ydl_opts, try to find it
        requested_format_id = self.ydl_opts.get("format")
        if requested_format_id:
            for fmt in formats:
                if fmt.get("format_id") == requested_format_id:
                    if fmt.get('protocol') in ('http', 'https'): # Ensure it's a downloadable http/s format
                        return fmt

        # Fallback to yt-dlp's default selection or a good streaming format
        # We want a single file, not a manifest like m3u8 for this class
        try:
            # Let yt-dlp determine the best format if not specified or found
            # We need to ensure it's a progressive download format
            temp_ydl_opts = self.ydl_opts.copy()
            temp_ydl_opts['skip_download'] = True # Already default but good to be explicit

            # Filter for progressive, http/https downloadable formats
            # This selector prioritizes MP4. You might want to adjust.
            # It also tries to avoid manifest formats if possible for this pseudo-streaming.
            # "bv*+ba/b" means best video with audio, or best overall if not available.
            # "[protocol^=http]" ensures it's downloadable.
            # "[vcodec!=none][acodec!=none]" ensures it has both video and audio.
            # We need to be more specific to avoid DASH manifests if filesize is None.

            # A simpler approach: iterate and find a good candidate
            # Prioritize non-fragmented, non-live, http/https formats with both codecs and known filesize.
            good_candidates = []
            for f in formats:
                # Stricter check: ensure it's not fragmented and has actual filesize
                if (f.get('vcodec') != 'none' and f.get('acodec') != 'none' and
                        f.get('protocol') in ('http', 'https') and
                        not f.get('is_live') and
                        not f.get('is_fragmented') and # Explicitly avoid fragmented
                        f.get('fragment_base_url') is None and # Another check for fragmentation
                        f.get('filesize') is not None): # Prioritize known filesize
                    good_candidates.append(f)

            if not good_candidates: # Fallback to allow filesize_approx if no exact filesize found
                 for f in formats:
                    if (f.get('vcodec') != 'none' and f.get('acodec') != 'none' and
                            f.get('protocol') in ('http', 'https') and
                            not f.get('is_live') and
                            not f.get('is_fragmented') and
                            f.get('fragment_base_url') is None and
                            (f.get('filesize') is not None or f.get('filesize_approx') is not None)):
                        good_candidates.append(f)


            if good_candidates:
                # Sort by filesize (descending) or some other preference if needed
                # Prefer exact filesize, then approximate. Then by resolution or bitrate if desired.
                good_candidates.sort(key=lambda f: (
                    f.get('filesize') or 0,
                    f.get('filesize_approx') or 0,
                    f.get('height') or 0 # Prefer higher resolution as a tie-breaker
                ), reverse=True)
                # print(f"Selected format: {good_candidates[0]}")
                return good_candidates[0]

            # If no "good" progressive found, try yt-dlp's internal logic for requested_formats
            # This part is from your original code and might pick a format even if not ideal.
            # But we should still filter it.
            try:
                processed_info = self.ydl.process_video_result(self.info_dict, download=False)
                selected_formats_list = processed_info.get("requested_formats")
                if selected_formats_list:
                    for sf in selected_formats_list:
                        # Ensure the selected format is also suitable (single file, http/s)
                        if (sf.get('vcodec') != 'none' and
                            sf.get('protocol') in ('http', 'https') and
                            not sf.get('is_fragmented') and
                            sf.get('fragment_base_url') is None):
                            # print(f"Selected format from requested_formats: {sf}")
                            return sf
                    # Fallback to the first in list if it's downloadable, but less ideal
                    if (selected_formats_list[0].get('protocol') in ('http', 'https') and
                        not selected_formats_list[0].get('is_fragmented')):
                        # print(f"Selected format (fallback from requested_formats): {selected_formats_list[0]}")
                        return selected_formats_list[0]

                # Fallback for cases where 'requested_formats' isn't populated but a single format is chosen by ytdl
                if ("format_id" in processed_info and
                    processed_info.get('protocol') in ('http', 'https') and
                    not processed_info.get('is_fragmented') and
                    processed_info.get('fragment_base_url') is None and
                    processed_info.get('vcodec') != 'none'): # Ensure it has video
                    # print(f"Selected format (processed_info fallback): {processed_info}")
                    return processed_info

            except Exception as e_proc:
                print(f"Warning: Error during yt-dlp process_video_result for format selection: {e_proc}")


            print(f"Warning: Could not determine a single, suitable progressive format. Formats available: {formats}")
            return None

        except (yt_dlp.utils.YtDlpError, KeyError) as e:
            print(f"Error selecting format with yt-dlp: {e}")
            return None

    async def _ensure_downloaded(self, start_byte: int, end_byte: Optional[int]):
        # This method needs to be async if it uses the asyncio lock
        # and if yt_dlp.download is run in an executor.
        # Your original code ran ydl_segment.download synchronously.
        # For FastAPI, long-running synchronous calls should be in run_in_executor.

        # Check if file exists and if the required range is already downloaded
        # This is a simplified check; real partial download management is complex.
        # For now, we assume yt-dlp's 'continuedl' and Range headers handle it.
        # A more robust solution would track downloaded segments.

        max_retries = 3 # Reduced for faster failure in some cases
        retry_delay = 1

        # Ensure end_byte is not None for the Range header if total_size is unknown for this segment
        # However, yt-dlp usually requires an end for Range requests.
        # If end_byte is truly unknown, we might download a fixed chunk ahead.
        effective_end_byte = end_byte
        if effective_end_byte is None: # If total size is unknown, download a speculative chunk
            effective_end_byte = start_byte + (5 * 1024 * 1024) # Download 5MB ahead if end is not known

        # Check if file exists and if the current known size covers the request
        # This is a very basic check. yt-dlp's continuedl is the main mechanism.
        try:
            if os.path.exists(self.filepath):
                current_file_size = await self.loop.run_in_executor(None, os.path.getsize, self.filepath)
                if current_file_size >= effective_end_byte + 1:
                    # print(f"Range {start_byte}-{effective_end_byte} seems to be already downloaded.")
                    return # Assume it's downloaded
        except FileNotFoundError:
            pass # File doesn't exist, proceed to download

        for attempt in range(max_retries):
            try:
                dl_opts = self.ydl_opts.copy()
                # Critical: Use the selected format's ID.
                # 'format' in ydl_opts might be a selector string, not the final ID.
                dl_opts.update({
                    "format": self.format["format_id"],
                    "outtmpl": self.download_outtmpl, # Use the template
                    "continuedl": True,
                    "noprogress": True,
                    "quiet": True,
                    # "ratelimit": "1M", # For testing to simulate slower downloads
                    # "fragment_retries": 10, # yt-dlp internal retries for fragments
                    # "retry_sleep_functions": {"http": lambda n: 0.5 * (2 ** n)}, # Exponential backoff for http
                })

                # Add Range header for partial download
                # yt-dlp might not use this if the server doesn't support it well,
                # or if continuedl is more effective for its strategy.
                # Forcing it via http_headers is a strong hint.
                # Conditional Range header:
                # Only apply if total_size is known and we have a specific end_byte for the current request segment.
                # If total_size is unknown, or if end_byte for this segment is speculative (e.g. initial large chunk),
                # let yt-dlp manage with continuedl, as precise Range might be problematic.
                if self.total_size and end_byte is not None: # 'end_byte' here is the original from caller, not 'effective_end_byte'
                    # Ensure effective_end_byte (which might be capped by total_size) is used.
                    # And start_byte must be less than effective_end_byte.
                    if effective_end_byte is not None and start_byte <= effective_end_byte:
                        dl_opts["http_headers"] = {"Range": f"bytes={start_byte}-{effective_end_byte}"}
                        # print(f"DEBUG: Using Range header: bytes={start_byte}-{effective_end_byte}")
                    else:
                        # This case (e.g. start_byte > effective_end_byte) should ideally be caught before attempting download.
                        # If effective_end_byte is None here but self.total_size is known, it means we want the rest of the file.
                        dl_opts["http_headers"] = {"Range": f"bytes={start_byte}-"}
                        # print(f"DEBUG: Using Range header: bytes={start_byte}-")
                # else:
                    # print(f"DEBUG: Not using explicit Range header. total_size: {self.total_size}, end_byte: {end_byte}")


                # print(f"Attempting to download range {start_byte}-{effective_end_byte} for {self.url} with opts: {dl_opts}")

                # Run synchronous yt-dlp download in an executor thread
                try:
                    with yt_dlp.YoutubeDL(dl_opts) as ydl_segment:
                        await self.loop.run_in_executor(None, ydl_segment.download, [self.url])
                except yt_dlp.utils.DownloadError as de:
                    # Log more details for DownloadError, especially for conflicting range
                    err_msg = str(de)
                    print(f"yt-dlp DownloadError occurred: {err_msg} for URL {self.url} with opts {dl_opts}")
                    if "conflicting range" in err_msg.lower():
                        print(f"Conflicting range error details: start_byte={start_byte}, effective_end_byte={effective_end_byte}, total_size={self.total_size}")
                    raise # Re-raise the original error to be handled by the outer loop

                # After download, update self.filepath to the actual downloaded file if outtmpl had placeholders
                # For simple 'video.ext', it should match.
                # If ydl.prepare_filename was used with info_dict after download, it would be more robust.
                # Let's try to find the downloaded file if our predicted self.filepath doesn't exist.
                if not os.path.exists(self.filepath):
                    # This logic assumes 'video.%(ext)s' was used.
                    # We need a more robust way to get the actual filename yt-dlp used.
                    # One way is to list dir and find the file, but that's hacky.
                    # The best is if yt-dlp could return the filename from download call, or if prepare_filename is reliable.
                    # For now, if self.filepath (predicted) doesn't exist, this might be an issue.
                    # Let's re-evaluate self.filepath based on the downloaded file if possible.
                    # This is tricky because yt-dlp might merge video and audio into a new file.
                    # If 'outtmpl' is fixed (no placeholders like title/id), it's simpler.
                    # Our current self.download_outtmpl is 'video.%(ext)s'.
                    # The actual extension comes from self.format['ext'].
                    # So self.filepath should be correct.

                    # Check if the file exists after download attempt
                    if not os.path.exists(self.filepath):
                         print(f"Warning: File {self.filepath} still not found after download attempt for {self.url}")
                         # raise FileNotFoundError(f"yt-dlp finished but file {self.filepath} not found.")
                         # Don't raise here, let read() fail if it can't open.
                return

            except yt_dlp.utils.DownloadError as e:
                err_str = str(e).lower()
                if "http error 416" in err_str or "requested range not satisfiable" in err_str:
                    # This can happen if the range is already fully downloaded or invalid
                    # print(f"HTTP 416 for {self.url}, range {start_byte}-{effective_end_byte}. Assuming complete or invalid.")
                    # If it's truly invalid, the client shouldn't have requested it.
                    # If it means "already have it", that's okay.
                    # We should check file size here.
                    if os.path.exists(self.filepath): # If file exists, maybe it's fine
                        return
                    # If it doesn't exist, then 416 is a problem
                    if attempt == max_retries - 1:
                        raise HTTPException(status_code=416, detail=f"Requested Range Not Satisfiable: {e}")

                # Common transient errors
                transient_errors = ["eof occurred in violation of protocol", "connection reset by peer", "ssl_handshake_error"]
                if any(sub in err_str for sub in transient_errors):
                    if attempt == max_retries - 1:
                        raise HTTPException(status_code=503, detail=f"yt-dlp download error after retries: {e}")
                    print(f"Download error (attempt {attempt + 1}/{max_retries}) for {self.url}: {e}. Retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay) # Use asyncio.sleep
                    retry_delay *= 2
                else:
                    raise HTTPException(status_code=502, detail=f"yt-dlp download error: {e}") from e

            except Exception as e:
                if attempt == max_retries - 1:
                    raise HTTPException(status_code=500, detail=f"Unexpected error in _ensure_downloaded after retries: {e}") from e
                print(f"Unexpected error (attempt {attempt + 1}/{max_retries}) for {self.url}: {e}. Retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 2


    async def _open_file(self):
        # This method should be async due to _ensure_downloaded
        async with self._lock: # Ensure only one coroutine tries to open/initially download
            if self._file is None:
                # Initial download for the very first part (e.g., first 1-2 MB for metadata)
                # The amount to download initially depends on where player typically reads metadata from.
                initial_chunk_size = 1 * 1024 * 1024 # 1MB
                end_byte_for_initial = initial_chunk_size -1
                if self.total_size and self.total_size < initial_chunk_size:
                    end_byte_for_initial = self.total_size - 1

                await self._ensure_downloaded(0, end_byte_for_initial if self.total_size else None) # Pass None if total_size unknown

                try:
                    self._file = await self.loop.run_in_executor(None, open, self.filepath, "rb")
                except FileNotFoundError:
                    # This shouldn't happen if _ensure_downloaded worked or raised.
                    # But as a fallback.
                    print(f"Error: File {self.filepath} not found after _ensure_downloaded for initial open.")
                    raise HTTPException(status_code=500, detail=f"Stream file {self.filepath} not found after download attempt.")
            return self._file

    async def read(self, size: int = -1) -> bytes:
        # This method should be async
        file = await self._open_file() # Ensure file is open and initial part downloaded
        if size == 0:
            return b""

        async with self._lock: # Lock for read operations that might trigger further downloads
            current_known_filesize = 0
            try:
                current_known_filesize = await self.loop.run_in_executor(None, os.path.getsize, self.filepath)
            except FileNotFoundError: # Should be caught by _open_file, but for safety:
                 # This implies the file disappeared after _open_file or initial download failed silently
                 print(f"File {self.filepath} vanished before read operation.")
                 return b"" # Or raise error


            # Determine the target end byte for this read operation
            if size == -1: # Read to end (or a large chunk if total_size is unknown)
                if self.total_size:
                    target_end_for_read = self.total_size -1
                    read_size = self.total_size - self._current_pos
                else:
                    # Read a large chunk if total size is unknown
                    read_size = 10 * 1024 * 1024
                    target_end_for_read = self._current_pos + read_size -1
            else:
                read_size = size
                target_end_for_read = self._current_pos + size - 1

            # If the known total size is available and target_end_for_read exceeds it, cap it.
            if self.total_size and target_end_for_read >= self.total_size:
                target_end_for_read = self.total_size - 1
                read_size = self.total_size - self._current_pos
                if read_size < 0: read_size = 0


            # Ensure the required part is downloaded if not already covered
            if target_end_for_read >= current_known_filesize and current_known_filesize < (self.total_size or float('inf')):
                # print(f"Read needs up to {target_end_for_read}, current file size {current_known_filesize}. Downloading more.")
                # Download from current_known_filesize up to target_end_for_read (or further if total_size unknown)
                # If total_size is None, _ensure_downloaded will download a speculative chunk.
                await self._ensure_downloaded(current_known_filesize, target_end_for_read if self.total_size else None)

            # Perform the actual read from the file
            await self.loop.run_in_executor(None, file.seek, self._current_pos)
            data = await self.loop.run_in_executor(None, file.read, read_size)

            self._current_pos += len(data)
            return data

    async def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        # This method can remain mostly synchronous in logic but uses async lock
        # as it modifies _current_pos which is used by async read.
        async with self._lock:
            if whence == io.SEEK_SET:
                new_pos = offset
            elif whence == io.SEEK_CUR:
                new_pos = self._current_pos + offset
            elif whence == io.SEEK_END:
                if self.total_size is None:
                    # If we don't know total size, SEEK_END is problematic.
                    # We could try to force a download of "everything" or raise.
                    # For now, let's try to estimate by downloading a large chunk if not already.
                    # This is complex. Simpler to raise or require total_size for SEEK_END.
                    print("Warning: SEEK_END used on stream with unknown total size. Trying to fetch more.")
                    await self._ensure_downloaded(self._current_pos, None) # Download a large chunk
                    # Re-check total_size if it got updated by _ensure_downloaded (if format info improved)
                    if not self.total_size:
                         raise ValueError("SEEK_END is not supported for unknown file size after download attempt.")
                    new_pos = self.total_size + offset

                else: # total_size is known
                    new_pos = self.total_size + offset
            else:
                raise ValueError("Invalid whence value. Use io.SEEK_SET, io.SEEK_CUR, or io.SEEK_END.")

            if new_pos < 0:
                # For pseudo-streaming, seeking before 0 doesn't make sense.
                # Standard file seek might allow it and then fail on read.
                new_pos = 0

            # If seeking beyond known total_size, cap it? Or let read handle it?
            # Standard seek allows this. Read will then return empty bytes.
            # if self.total_size is not None and new_pos > self.total_size:
            #    new_pos = self.total_size

            self._current_pos = new_pos
            # print(f"Seeked to {self._current_pos}")
            return self._current_pos

    def tell(self) -> int:
        # This can be synchronous as it just returns a variable
        return self._current_pos

    async def close(self):
        # This should be async if file close is blocking, or use run_in_executor
        async with self._lock:
            if self._file:
                await self.loop.run_in_executor(None, self._file.close)
                self._file = None

            # Cleanup the unique directory for this stream instance
            if self.stream_instance_dir and os.path.exists(self.stream_instance_dir):
                try:
                    # Use shutil.rmtree for directories
                    import shutil
                    max_cleanup_attempts = 3
                    cleanup_delay = 0.5 # seconds
                    for attempt in range(max_cleanup_attempts):
                        try:
                            await self.loop.run_in_executor(None, shutil.rmtree, self.stream_instance_dir)
                            # print(f"Cleaned up temp directory: {self.stream_instance_dir} (attempt {attempt + 1})")
                            break # Success
                        except Exception as e_cleanup:
                            if attempt == max_cleanup_attempts - 1:
                                print(f"Error cleaning up temp directory {self.stream_instance_dir} after {max_cleanup_attempts} attempts: {e_cleanup}")
                            else:
                                # print(f"Cleanup attempt {attempt + 1} failed for {self.stream_instance_dir}: {e_cleanup}. Retrying in {cleanup_delay}s...")
                                await asyncio.sleep(cleanup_delay)
                except Exception as e: # Should be caught by the inner try-except now
                    print(f"Unexpected error during temp directory cleanup logic for {self.stream_instance_dir}: {e}")

# --- Helper functions from your code (can be part of streaming.py) ---

def parse_range_header(range_header: str, total_size: Optional[int]) -> tuple[int, int]:
    """Parses a Range header string (e.g., "bytes=0-1023") into start and end bytes."""
    if not range_header or not range_header.startswith("bytes="):
        raise ValueError("Invalid Range header format")

    range_spec = range_header.split("=")[1]
    start_str, end_str = range_spec.split("-") if "-" in range_spec else (range_spec, "")

    try:
        start = int(start_str)
    except ValueError:
        raise ValueError("Invalid start byte in Range header")

    if start < 0:
        raise ValueError("Start byte cannot be negative")

    if total_size is not None and start >= total_size:
        # This is a case for HTTP 416 Range Not Satisfiable
        raise HTTPException(status_code=416, detail="Range request start offset is past the end of the file.")


    if end_str: # "bytes=0-100"
        try:
            # HTTP Range end is inclusive, so if client sends "bytes=0-99", they want 100 bytes.
            # Our internal 'end' will be exclusive for slicing or length calculation.
            # Or, more simply, length = end_inclusive - start + 1
            end_inclusive = int(end_str)
        except ValueError:
            raise ValueError("Invalid end byte in Range header")

        if end_inclusive < start:
            raise ValueError("End byte cannot be less than start byte")

        if total_size is not None:
            end_inclusive = min(end_inclusive, total_size - 1)

        # For our internal use, let's define `end` as the byte *after* the last one requested.
        # So, if range is "0-99", length is 100. start=0, end_for_slicing=100.
        # However, the function returns the inclusive end as per HTTP.
        # Let's clarify: this function should return the *inclusive* end byte for Content-Range.
        # The actual number of bytes to send is (end_inclusive - start + 1).
        return start, end_inclusive

    else: # "bytes=100-" (from start to end of file)
        # This block is entered if end_str is empty.
        # Example: "bytes=100-" or "bytes=0-"

        if total_size is None:
            # If the total size is unknown, a range like "bytes=N-" cannot be precisely satisfied
            # in terms of setting a Content-Range header for the full range.
            # The stream itself might handle it by streaming until EOF, but parse_range_header
            # needs to return defined start and end for header construction.
            # Raising an error here is consistent if the caller expects to use the returned
            # end value for a Content-Range header with a defined total.
            raise ValueError("Range 'bytes=N-' requires a known total file size to determine the end boundary for the Content-Range header.")

        # At this point, total_size is NOT None.
        # Also, the earlier check `if total_size is not None and start >= total_size:`
        # would have raised an HTTPException if `start` was at or beyond the end of the file.
        # Therefore, we can assume `start < total_size`.
        # Since `start` is non-negative, this also implies `total_size > 0`.
        # The case `total_size == 0` (and `start == 0`) would have been caught by that prior check, raising 416.
        # Thus, we can directly calculate the end as total_size - 1.
        return start, total_size - 1 # end is inclusive


# Placeholder for the actual FastAPI app instance if we define routes here
# from fastapi import APIRouter
# stream_router = APIRouter()
# @stream_router.get(...)
# We will integrate this into the main app's video router or a new streaming router.

    # This line should ideally not be reached if the logic is correct and all paths return or raise.
    # If it's reached, it indicates a flaw in the conditional logic above.
    raise RuntimeError("Internal logic error: parse_range_header reached end without returning a value or raising an exception.")
