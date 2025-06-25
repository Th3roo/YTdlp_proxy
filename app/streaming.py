import io
import os
import re
import time
from typing import Annotated, Optional, Any, Dict # Added Optional, Any, Dict for YTDLPSeekableStream annotations
import uuid # Added for unique filenames, keep for now
import asyncio # Added for async operations, keep for now

# FastAPI specific imports are not strictly needed here anymore as class is used by API layer
# from fastapi import HTTPException # HTTPException is used
from fastapi import HTTPException # HTTPException is used directly
import yt_dlp

# Директория для временных частей видео - ОСТАВЛЕНО ИЗ СТАРОГО КОДА, МОЖЕТ ПРИГОДИТЬСЯ
TEMP_VIDEO_PARTS_DIR = "temp_video_parts"
os.makedirs(TEMP_VIDEO_PARTS_DIR, exist_ok=True)

def get_safe_filename(name: str) -> str: # ОСТАВЛЕНО ИЗ СТАРОГО КОДА
    """ Sanitize a string to be used as a filename. """
    name = re.sub(r'[^\w\s-]', '', name).strip()
    name = re.sub(r'[-\s]+', '-', name)
    return name if len(name) <= 200 else name[:200] # Limit length


class YTDLPSeekableStream:

    def __init__(self, url: str, ydl_opts: dict = None, loop: Optional[asyncio.AbstractEventLoop] = None): # Added loop for compatibility with existing code structure if needed
        self.url = url
        self.ydl_opts = ydl_opts or {}
        # Ensure some defaults for streaming, these can be overridden by passed ydl_opts
        self.ydl_opts.setdefault('noplaylist', True)
        self.ydl_opts.setdefault('quiet', True)
        self.ydl_opts.setdefault('noprogress', True)
        # Override format for streaming to the most general 'best'
        self.ydl_opts['format'] = 'best'
        # self.ydl_opts.setdefault('debug_printtraffic', True) # For debugging if needed

        self.ydl = yt_dlp.YoutubeDL(self.ydl_opts)
        self.loop = loop or asyncio.get_event_loop()

        # Initialize filepath *immediately* - From your new code
        # This initial filepath might be temporary if title/id are not yet known
        # or if yt-dlp generates a more specific name later.
        # The key is that self.filepath must be defined before any operation that might use it.

        # We need a unique identifier for the temporary file/directory for this stream instance
        # to avoid collisions if multiple streams are processed concurrently.
        self.stream_instance_id = str(uuid.uuid4())
        self.instance_temp_dir = os.path.join(TEMP_VIDEO_PARTS_DIR, self.stream_instance_id)
        os.makedirs(self.instance_temp_dir, exist_ok=True)

        # Initial dummy filepath, will be refined after info_dict extraction
        # The '.ytdlp' extension was in your original code, let's keep it for consistency
        # if it signifies a special handling or partial download state.
        # yt-dlp itself will add the correct extension based on the format.
        # So, using a base name inside the unique directory.
        self.base_filename_in_dir = "video_part" # A generic base name
        self.filepath = os.path.join(self.instance_temp_dir, self.base_filename_in_dir + ".ytdlp") # Initial dummy path

        self._current_pos = 0
        self._file = None # File handle

        # Extract info (synchronously for now, as in your original code)
        try:
            # print(f"YTDLPSeekableStream: Extracting info for URL: {self.url} with opts: {self.ydl_opts}")
            self.info_dict = self.ydl.extract_info(url, download=False)
            if not self.info_dict:
                self._cleanup_temp_dir() # Clean up if info extraction fails fundamentally
                raise HTTPException(status_code=404, detail="Video metadata not found (yt-dlp info_dict is None)")
        except yt_dlp.utils.DownloadError as e:
            self._cleanup_temp_dir()
            # print(f"YTDLPSeekableStream: yt-dlp DownloadError during info extraction: {e}")
            if "Unsupported URL" in str(e):
                 raise HTTPException(status_code=400, detail=f"Unsupported URL: {self.url}")
            elif "Video unavailable" in str(e): # Common error message
                 raise HTTPException(status_code=404, detail=f"Video unavailable: {e}")
            else:
                 raise HTTPException(status_code=502, detail=f"yt-dlp failed to extract video info: {e}") # Bad Gateway or similar
        except Exception as e:
            self._cleanup_temp_dir()
            # print(f"YTDLPSeekableStream: Unexpected error during info extraction: {e}")
            raise HTTPException(status_code=500, detail=f"Unexpected error extracting video info: {e}")


        self.format = self._get_selected_format() # Uses self.info_dict and self.ydl_opts
        if not self.format:
            self._cleanup_temp_dir()
            raise HTTPException(status_code=400, detail="No suitable streamable format found by yt-dlp.")

        # Update/refine filepath with the *correct* filename based on extracted info and format
        # yt-dlp's prepare_filename can give us the expected filename.
        # We need to pass an info_dict that yt-dlp can use (usually the one for the selected format).
        # The outtmpl should point to our unique directory.
        temp_format_info_for_prepare = self.format.copy() # Use a copy of the selected format
        temp_format_info_for_prepare['id'] = self.info_dict.get('id', 'temp_id')
        temp_format_info_for_prepare['title'] = self.info_dict.get('title', 'temp_title')
        # Ensure 'ext' is present in the format info, as prepare_filename uses it.
        if 'ext' not in temp_format_info_for_prepare:
            temp_format_info_for_prepare['ext'] = self.format.get('video_ext', self.format.get('audio_ext', 'mp4'))


        # Define outtmpl for yt-dlp download operations. This will be the final path.
        # We use a fixed name within our unique instance directory.
        # yt-dlp will add the correct extension based on the format.
        self.download_outtmpl = os.path.join(self.instance_temp_dir, self.base_filename_in_dir + '.%(ext)s')

        # Predict the final filepath based on this outtmpl and the selected format's extension
        # This is crucial for opening the file later.
        final_ext = self.format.get('ext', 'mp4') # Default to mp4 if ext not in format
        self.filepath = os.path.join(self.instance_temp_dir, self.base_filename_in_dir + f'.{final_ext}')
        # print(f"YTDLPSeekableStream: Final filepath set to: {self.filepath}")


        self.total_size = self.format.get("filesize") or self.format.get("filesize_approx")
        if not self.total_size and self.format.get('fragments'):
             # This class is mainly for progressive downloads.
             # If it's a manifest (HLS/DASH) and filesize is None, Range requests are problematic.
             # yt-dlp might download the manifest itself as the "file" if not handled.
             # For now, we proceed, but total_size being None will affect client behavior.
             # print(f"Warning: Format {self.format.get('format_id')} is fragmented or filesize is unknown.")
             pass

        self._lock = asyncio.Lock() # For async operations, if any are introduced later or for file access


    def _get_selected_format(self): # From your new code, with minor adjustments for self.ydl_opts
        formats = self.info_dict.get("formats", [])
        if not formats:
            return None

        # Check if a specific format ID was requested in ydl_opts (e.g., from client)
        # This should be the primary way to select if provided.
        requested_format_selector = self.ydl_opts.get("format") # This might be a selector string or format_id

        if requested_format_selector:
            # Try to find a direct match for format_id first
            for fmt in formats:
                if fmt.get("format_id") == requested_format_selector:
                    if fmt.get('protocol') in ('http', 'https'): # Ensure it's downloadable
                        # print(f"DEBUG: Directly matched requested format_id: {requested_format_selector}")
                        return fmt
            # If not a direct ID match, yt-dlp's process_video_result will use the selector
            # print(f"DEBUG: Using format selector: {requested_format_selector}")

        # Fallback selection logic if no specific format was requested or matched directly
        # This part is similar to your original logic for default selection.
        # We want a single, progressive, downloadable file.
        try:
            # Let yt-dlp process the info_dict with the given format selector (or its default if none)
            # This will populate 'requested_formats' or select a single format.
            # Crucially, we need to ensure this processing doesn't initiate a download.
            processed_info = self.ydl.process_video_result(self.info_dict.copy(), download=False)

            # Check 'requested_formats' first (if populated by a selector like "bestvideo+bestaudio")
            selected_formats_list = processed_info.get("requested_formats")
            if selected_formats_list:
                # This list contains formats chosen by yt-dlp to satisfy the selector.
                # For streaming, we typically need a single merged format if video+audio was requested.
                # The first format in this list is usually the merged one or the best single one.
                chosen_fmt = selected_formats_list[0]
                if (chosen_fmt.get('protocol') in ('http', 'https') and
                    not chosen_fmt.get('is_fragmented') and # Avoid native HLS/DASH manifests
                    chosen_fmt.get('fragment_base_url') is None):
                    # print(f"DEBUG: Selected format from 'requested_formats': {chosen_fmt.get('format_id')}")
                    return chosen_fmt
                else:
                    # print(f"DEBUG: Format from 'requested_formats' ({chosen_fmt.get('format_id')}) is not suitable (e.g. fragmented).")
                    pass # Fall through to other checks

            # If 'requested_formats' is not there or not suitable, check if 'processed_info' itself is the chosen format
            if ("format_id" in processed_info and
                processed_info.get('protocol') in ('http', 'https') and
                not processed_info.get('is_fragmented') and
                processed_info.get('fragment_base_url') is None):
                # This happens if format selector was for a single stream or yt-dlp resolved to one.
                # print(f"DEBUG: Selected format from processed_info itself: {processed_info.get('format_id')}")
                return processed_info

            # If still no suitable format, log and return None
            # print(f"DEBUG: No suitable single, progressive, HTTP/S format found after processing. Available formats: {len(formats)}")
            # for f_idx, f_val in enumerate(formats):
            #     print(f"  Format {f_idx}: id={f_val.get('format_id')}, proto={f_val.get('protocol')}, frag={f_val.get('is_fragmented')}, ext={f_val.get('ext')}")

            # Fallback: iterate through all formats and pick the best available progressive one if the above failed
            # This is a more generic fallback.
            best_fallback = None
            for fmt in sorted(formats, key=lambda f: (f.get('height', 0), f.get('tbr', 0)), reverse=True):
                if (fmt.get('protocol') in ('http', 'https') and
                    not fmt.get('is_fragmented') and
                    fmt.get('fragment_base_url') is None and
                    fmt.get('vcodec') != 'none'): # Ensure it has video
                    # print(f"DEBUG: Using fallback selection: Found suitable format {fmt.get('format_id')}")
                    best_fallback = fmt
                    break
            if best_fallback:
                return best_fallback

            # print("Warning: _get_selected_format: No suitable format found after all checks.")
            return None

        except (yt_dlp.utils.YtDlpError, KeyError) as e_proc:
            # print(f"Warning: Error during yt-dlp process_video_result for format selection: {e_proc}")
            return None


    async def _ensure_downloaded(self, start_byte: int, end_byte: Optional[int]): # Made async
        # This method is now async and uses run_in_executor for blocking yt-dlp calls.
        max_retries = 5
        retry_delay = 1

        # Determine the actual end byte for the Range header for this download operation
        # If end_byte is None (e.g. unknown total size, initial large chunk request),
        # yt-dlp might download a fixed chunk or up to where it can.
        # For 'continuedl', precise Range might not always be needed if the file already exists partially.
        range_header_val = None
        if end_byte is not None:
            if start_byte <= end_byte: # Ensure valid range
                range_header_val = f"bytes={start_byte}-{end_byte}"
        # else: If end_byte is None, we might want to download from start_byte onwards without specifying end.
        # Some servers support "bytes=start_byte-". yt-dlp's internal logic handles this.
        # Forcing via http_headers is a strong hint.
        # If we don't set range_header_val, yt-dlp might try to download the whole thing,
        # but 'continuedl' should pick up if file exists.

        # Check if the file exists and if the requested segment seems to be covered.
        # This is a basic check. yt-dlp's 'continuedl' is the primary mechanism.
        # We need to be careful if end_byte is far ahead or None.
        try:
            if os.path.exists(self.filepath):
                current_file_size = await self.loop.run_in_executor(None, os.path.getsize, self.filepath)
                # If end_byte is specified and current_file_size covers it, assume it's downloaded.
                if end_byte is not None and current_file_size >= end_byte + 1:
                    # print(f"DEBUG: Range {start_byte}-{end_byte} appears to be already downloaded (file size: {current_file_size}).")
                    return
                # If end_byte is None, and we are seeking (start_byte > 0), and file exists,
                # assume previous parts are there and yt-dlp will continue.
                # This part is tricky without knowing exactly what's needed.
        except FileNotFoundError:
            pass # File doesn't exist, proceed to download.


        for attempt in range(max_retries):
            try:
                dl_opts = self.ydl_opts.copy()
                dl_opts.update({
                    "format": self.format["format_id"], # Use the specifically selected format ID
                    "outtmpl": self.download_outtmpl,   # Use the outtmpl with extension placeholder
                    "continuedl": True,
                    "noprogress": True,
                    "quiet": True,
                    # "ratelimit": "500K", # For testing slower downloads
                    # "fragment_retries": 10, # yt-dlp internal retries
                    # "retry_sleep_functions": {"http": lambda n: 0.5 * (2 ** n)},
                })

                # Conditionally add Range header. yt-dlp also has --download-sections.
                # Using http_headers for Range is more direct for some servers.
                if range_header_val:
                    dl_opts["http_headers"] = {"Range": range_header_val}
                    # print(f"DEBUG: Attempt {attempt+1}: Downloading with Range: {range_header_val} for {self.url}")
                # else:
                    # print(f"DEBUG: Attempt {attempt+1}: Downloading (no explicit Range header, relying on continuedl) for {self.url}")


                # Run synchronous yt-dlp download in an executor thread
                with yt_dlp.YoutubeDL(dl_opts) as ydl_segment:
                    await self.loop.run_in_executor(None, ydl_segment.download, [self.url])

                # After download, verify the file exists. self.filepath should be correct now.
                if not await self.loop.run_in_executor(None, os.path.exists, self.filepath):
                    # This is unexpected if ydl.download completed without error.
                    # print(f"ERROR: File {self.filepath} not found after yt-dlp download completed for {self.url}.")
                    # It might be that the extension predicted was wrong, or yt-dlp named it differently.
                    # We could try to find the file in self.instance_temp_dir if this happens.
                    # For now, assume self.filepath prediction is correct.
                    # If this error occurs, the logic for self.filepath generation needs review.
                    raise FileNotFoundError(f"Download finished but target file {self.filepath} not found.")
                return # Success

            except yt_dlp.utils.DownloadError as e:
                err_str = str(e).lower()
                # print(f"DEBUG: yt-dlp DownloadError (attempt {attempt+1}): {err_str} for URL {self.url}")

                if "http error 416" in err_str or "requested range not satisfiable" in err_str:
                    # This means the server cannot satisfy the range.
                    # It could be because the range is invalid, or the content is already fully there.
                    # If the file exists, we can assume it's okay for continuedl.
                    if await self.loop.run_in_executor(None, os.path.exists, self.filepath):
                        # print(f"DEBUG: HTTP 416, but file {self.filepath} exists. Assuming segment is available or issue is with range spec.")
                        return # Assume okay if file exists, let read handle it.
                    if attempt == max_retries - 1:
                        self._cleanup_temp_dir()
                        raise HTTPException(status_code=416, detail=f"Requested Range Not Satisfiable: {e}")

                transient_errors = [
                    "eof occurred in violation of protocol", "connection reset by peer",
                    "ssl_handshake_error", "urlopen error [errno 110] connection timed out",
                    "read error [errno 104] connection reset by peer" # Common on poor connections
                ]
                if any(sub in err_str for sub in transient_errors):
                    if attempt == max_retries - 1:
                        self._cleanup_temp_dir()
                        raise HTTPException(status_code=503, detail=f"yt-dlp download error after retries (transient): {e}")
                    # print(f"Download error (attempt {attempt + 1}/{max_retries}, transient) for {self.url}: {e}. Retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                else: # Non-transient DownloadError
                    self._cleanup_temp_dir()
                    raise HTTPException(status_code=502, detail=f"yt-dlp download error (non-transient): {e}") from e

            except FileNotFoundError as e: # Catch the FileNotFoundError we might raise above
                self._cleanup_temp_dir()
                raise HTTPException(status_code=500, detail=f"Failed to find downloaded file: {e}") from e

            except Exception as e:
                # print(f"DEBUG: Unexpected error in _ensure_downloaded (attempt {attempt+1}): {e}")
                if attempt == max_retries - 1:
                    self._cleanup_temp_dir()
                    raise HTTPException(status_code=500, detail=f"An unexpected error occurred in _ensure_downloaded after retries: {e}") from e
                # print(f"Unexpected error (attempt {attempt + 1}/{max_retries}) for {self.url}: {e}. Retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 2


    async def _open_file(self): # Made async
        # This method should be async due to _ensure_downloaded and file ops in executor
        async with self._lock:
            if self._file is None:
                # Ensure at least the initial part of the file is downloaded.
                # This helps in getting metadata embedded in the file if needed by the player quickly.
                # Download a small initial chunk (e.g., 1-2MB) unless total_size is smaller.
                initial_chunk_size = 1 * 1024 * 1024 # 1MB
                end_byte_for_initial = initial_chunk_size - 1
                if self.total_size and self.total_size > 0 and self.total_size < initial_chunk_size:
                    end_byte_for_initial = self.total_size - 1
                elif self.total_size == 0: # Handle 0-byte files (should be rare for video)
                    end_byte_for_initial = -1 # Indicates no download needed if file is truly 0 bytes

                if end_byte_for_initial >= 0 : # Only download if there's something to download
                    # print(f"DEBUG: _open_file: Ensuring initial download (0-{end_byte_for_initial}) for {self.filepath}")
                    await self._ensure_downloaded(0, end_byte_for_initial)
                # else:
                    # print(f"DEBUG: _open_file: Skipping initial download for 0-byte or invalid size file ({self.filepath})")


                try:
                    # print(f"DEBUG: _open_file: Opening file {self.filepath} in 'rb' mode.")
                    self._file = await self.loop.run_in_executor(None, open, self.filepath, "rb")
                except FileNotFoundError:
                    # This can happen if _ensure_downloaded failed to create the file,
                    # or if the file is 0 bytes and was never created, or if prediction of filepath was wrong.
                    # print(f"ERROR: _open_file: File {self.filepath} not found after _ensure_downloaded attempt.")
                    # Attempt to create an empty file if it was supposed to be 0 bytes and doesn't exist
                    if self.total_size == 0 and end_byte_for_initial == -1:
                        try:
                            # print(f"DEBUG: _open_file: Creating empty file for 0-byte content at {self.filepath}")
                            await self.loop.run_in_executor(None, open, self.filepath, "ab").close() # Create empty file
                            self._file = await self.loop.run_in_executor(None, open, self.filepath, "rb")
                        except Exception as e_create:
                            # print(f"ERROR: _open_file: Failed to create or open 0-byte file {self.filepath}: {e_create}")
                            self._cleanup_temp_dir()
                            raise HTTPException(status_code=500, detail=f"Stream file {self.filepath} could not be opened or created (0-byte case).")
                    else:
                        self._cleanup_temp_dir()
                        raise HTTPException(status_code=500, detail=f"Stream file {self.filepath} not found after download attempt.")
                except Exception as e_open: # Other errors during open
                    # print(f"ERROR: _open_file: Could not open file {self.filepath}: {e_open}")
                    self._cleanup_temp_dir()
                    raise HTTPException(status_code=500, detail=f"Could not open stream file {self.filepath}: {e_open}")

            return self._file


    async def read(self, size: int = -1) -> bytes: # Made async
        # This method is async due to _open_file and potential _ensure_downloaded calls.
        file_handle = await self._open_file() # Ensures file is open and initial part downloaded.
        if size == 0:
            return b""
        if not file_handle: # Should have been caught by _open_file raising HTTPException
             # print("ERROR: read: file_handle is None, _open_file should have raised.")
             return b"" # Should not happen

        async with self._lock: # Lock for read operations that might trigger further downloads
            current_file_size_on_disk = 0
            try:
                # Get current actual size on disk, it might have grown since last check.
                current_file_size_on_disk = await self.loop.run_in_executor(None, os.path.getsize, self.filepath)
            except FileNotFoundError:
                 # File disappeared after open? Highly unlikely with lock, but defensive.
                 # print(f"ERROR: read: File {self.filepath} vanished before read operation after being opened.")
                 return b"" # Or raise

            # Determine the target end position for this read operation on the stream
            # target_read_end_stream_pos is the stream position *after* this read completes.
            if size == -1: # Read until end of known total_size, or a large chunk if unknown
                if self.total_size is not None:
                    # We want to read up to self.total_size
                    # The number of bytes to request from file.read()
                    bytes_to_request_from_file = self.total_size - self._current_pos
                    target_read_end_stream_pos = self.total_size
                else:
                    # Total size unknown, read a speculative large chunk
                    bytes_to_request_from_file = 10 * 1024 * 1024 # 10MB
                    target_read_end_stream_pos = self._current_pos + bytes_to_request_from_file
            else: # Specific size requested
                bytes_to_request_from_file = size
                target_read_end_stream_pos = self._current_pos + size

            # Cap read if it goes beyond known total_size
            if self.total_size is not None and target_read_end_stream_pos > self.total_size:
                target_read_end_stream_pos = self.total_size
                bytes_to_request_from_file = self.total_size - self._current_pos

            if bytes_to_request_from_file < 0: # Should not happen if logic is correct
                bytes_to_request_from_file = 0

            if bytes_to_request_from_file == 0:
                return b"" # Nothing to read based on current pos and total_size

            # The actual end byte index we need on disk for this read: target_read_end_stream_pos - 1
            # (e.g. if current_pos=0, size=100, target_read_end_stream_pos=100, need bytes 0-99 on disk)
            required_disk_byte_idx = target_read_end_stream_pos - 1


            # Ensure the required part is downloaded if not already covered by current_file_size_on_disk
            # We need data up to `required_disk_byte_idx`.
            # Download if `required_disk_byte_idx` is beyond or at `current_file_size_on_disk`.
            # (i.e. `current_file_size_on_disk` needs to be at least `required_disk_byte_idx + 1`)
            if required_disk_byte_idx >= current_file_size_on_disk:
                # And also ensure we are not trying to download beyond total_size if known
                if not (self.total_size is not None and self._current_pos >= self.total_size) :
                    # print(f"DEBUG: read: Need byte {required_disk_byte_idx}, disk size {current_file_size_on_disk}. Downloading segment.")
                    # Download from current_file_size_on_disk up to required_disk_byte_idx.
                    # Or, if total_size is unknown, _ensure_downloaded will download a speculative chunk from current_file_size_on_disk.
                    # The start_byte for download should be where the current file ends on disk.
                    download_start_byte = current_file_size_on_disk
                    download_end_byte = required_disk_byte_idx
                    if self.total_size is None: # If total size unknown, pass None for end_byte to download a chunk
                        download_end_byte = None

                    await self._ensure_downloaded(download_start_byte, download_end_byte)


            # Perform the actual read from the file
            await self.loop.run_in_executor(None, file_handle.seek, self._current_pos)
            # print(f"DEBUG: read: Reading {bytes_to_request_from_file} bytes from {self.filepath} at offset {self._current_pos}")
            data = await self.loop.run_in_executor(None, file_handle.read, bytes_to_request_from_file)
            # print(f"DEBUG: read: Got {len(data)} bytes.")

            self._current_pos += len(data)
            return data


    async def seek(self, offset: int, whence: int = io.SEEK_SET) -> int: # Made async for lock
        # This method can remain mostly synchronous in logic but uses async lock
        # as it modifies _current_pos which is used by async read.
        async with self._lock:
            if whence == io.SEEK_SET:
                new_pos = offset
            elif whence == io.SEEK_CUR:
                new_pos = self._current_pos + offset
            elif whence == io.SEEK_END:
                if self.total_size is None:
                    # SEEK_END is problematic if total_size is unknown.
                    # We could try to force a download of "everything" to discover size, but that's risky.
                    # Best to raise an error or make it clear this is not fully supported.
                    # For now, let's attempt to download a chunk to see if total_size gets populated
                    # or if the file grows sufficiently. This is heuristic.
                    # print("Warning: SEEK_END used on stream with unknown total size. Attempting to download more to discover size.")
                    # Try to download from current pos to an arbitrary further point, or let _ensure_downloaded decide
                    # This might be a large download if not careful.
                    # Let's try to download a small chunk from current known end of file on disk.
                    current_disk_size = 0
                    if os.path.exists(self.filepath):
                        current_disk_size = await self.loop.run_in_executor(None, os.path.getsize, self.filepath)
                    await self._ensure_downloaded(current_disk_size, None) # Download a speculative chunk from end of current file

                    # Re-check total_size if it got updated (e.g., if format info improved or file fully downloaded)
                    # This is optimistic. yt-dlp doesn't usually update self.total_size post-initialization.
                    if self.total_size is None: # If still unknown
                        # Fallback: if file exists, use its current size on disk as a proxy for total_size.
                        # This is only safe if we believe the file is now complete.
                        if os.path.exists(self.filepath):
                             current_disk_size_after_dl = await self.loop.run_in_executor(None, os.path.getsize, self.filepath)
                             # Heuristic: if a significant download happened, maybe it's all there?
                             # This is not very reliable.
                             # print(f"Warning: Total size still unknown after download attempt. Using current disk size {current_disk_size_after_dl} for SEEK_END.")
                             # self.total_size = current_disk_size_after_dl # Tentatively set it
                             # Better to raise if it's still None.
                             self._cleanup_temp_dir()
                             raise ValueError("SEEK_END is not reliably supported when total file size is unknown and couldn't be determined.")
                    # If self.total_size became known, proceed:
                    new_pos = self.total_size + offset

                else: # total_size is known
                    new_pos = self.total_size + offset
            else:
                self._cleanup_temp_dir()
                raise ValueError("Invalid whence value. Use io.SEEK_SET, io.SEEK_CUR, or io.SEEK_END.")

            if new_pos < 0:
                new_pos = 0 # Standard behavior for seek

            # If seeking beyond known total_size, cap it to total_size?
            # Standard file seek allows seeking beyond EOF; read then returns empty.
            # Let's mimic that. If new_pos > self.total_size, reads from there should yield b"".
            # if self.total_size is not None and new_pos > self.total_size:
            #    new_pos = self.total_size

            self._current_pos = new_pos
            # print(f"DEBUG: seek: Stream position set to {self._current_pos}")
            return self._current_pos

    def tell(self) -> int:
        # This can be synchronous as it just returns a variable protected by the lock indirectly.
        return self._current_pos

    def _cleanup_temp_dir(self):
        # Synchronous cleanup helper, call from non-async contexts if needed or convert to async
        if hasattr(self, 'instance_temp_dir') and os.path.exists(self.instance_temp_dir):
            try:
                import shutil
                shutil.rmtree(self.instance_temp_dir)
                # print(f"DEBUG: Cleaned up temp directory: {self.instance_temp_dir}")
            except Exception as e_cleanup:
                # print(f"Warning: Error cleaning up temp directory {self.instance_temp_dir}: {e_cleanup}")
                pass # Non-critical, log and continue

    async def close(self): # Made async
        # This should be async for file close in executor and lock.
        async with self._lock: # Ensure exclusive access for closing
            if self._file:
                try:
                    await self.loop.run_in_executor(None, self._file.close)
                    # print(f"DEBUG: Closed file handle for {self.filepath}")
                except Exception as e_close:
                    # print(f"Warning: Error closing file {self.filepath}: {e_close}")
                    pass # Log and continue to directory cleanup
                finally:
                    self._file = None # Mark as closed

            # Cleanup the unique temporary directory for this stream instance
            if hasattr(self, 'instance_temp_dir') and await self.loop.run_in_executor(None, os.path.exists, self.instance_temp_dir):
                try:
                    import shutil
                    # print(f"DEBUG: Attempting to clean up temp directory: {self.instance_temp_dir}")
                    await self.loop.run_in_executor(None, shutil.rmtree, self.instance_temp_dir)
                    # print(f"DEBUG: Successfully cleaned up temp directory: {self.instance_temp_dir}")
                except Exception as e_cleanup:
                    # print(f"Warning: Error cleaning up temp directory {self.instance_temp_dir} during close: {e_cleanup}")
                    pass # Non-critical for the stream's immediate operation, but good to log.


# --- Helper functions (parse_range_header from your new code) ---

def parse_range_header(range_header: str, total_size: Optional[int]) -> tuple[int, int]: # Return type changed to inclusive end
    """
    Parses a Range header string (e.g., "bytes=0-1023") into start and *inclusive* end bytes.
    Raises HTTPException(416) if the range is invalid or unsatisfiable.
    """
    if not range_header or not range_header.lower().startswith("bytes="):
        # print(f"DEBUG: parse_range_header: Invalid format or missing 'bytes=': {range_header}")
        raise HTTPException(status_code=400, detail="Invalid Range header format: Must start with 'bytes='")

    range_spec = range_header.split("=")[1]
    parts = range_spec.split("-")
    start_str = parts[0]
    end_str = parts[1] if len(parts) > 1 and parts[1] else None # Handle "bytes=100-"

    try:
        start = int(start_str)
    except ValueError:
        # print(f"DEBUG: parse_range_header: Invalid start byte: {start_str}")
        raise HTTPException(status_code=400, detail="Invalid start byte in Range header")

    if start < 0:
        # print(f"DEBUG: parse_range_header: Start byte cannot be negative: {start}")
        raise HTTPException(status_code=416, detail="Start byte in Range header cannot be negative.")


    # If total_size is known, validate start against it.
    if total_size is not None:
        if start >= total_size:
            # This is a common case for 416.
            # print(f"DEBUG: parse_range_header: Start byte {start} is at or after end of content ({total_size}). Raising 416.")
            raise HTTPException(status_code=416, detail=f"Range start offset {start} is beyond content length {total_size}.")

    # Determine end_inclusive
    if end_str: # e.g., "bytes=0-100" or "bytes=50-50"
        try:
            end_inclusive = int(end_str)
        except ValueError:
            # print(f"DEBUG: parse_range_header: Invalid end byte: {end_str}")
            raise HTTPException(status_code=400, detail="Invalid end byte in Range header")

        if end_inclusive < start:
            # print(f"DEBUG: parse_range_header: End byte {end_inclusive} < start byte {start}. Raising 416.")
            raise HTTPException(status_code=416, detail="End byte in Range header cannot be less than start byte.")

        if total_size is not None:
            # Cap end_inclusive at the last available byte of the content.
            end_inclusive = min(end_inclusive, total_size - 1)
            # After capping, it's possible end_inclusive < start if original end_inclusive was valid but total_size was small.
            # Example: Range "bytes=50-100" for a file of size 60. Start=50. Capped end_inclusive becomes 59. Valid.
            # Example: Range "bytes=70-100" for a file of size 60. Start=70. This should have been caught by `start >= total_size`.
            # Let's re-check if, after capping, end < start. This implies the capped range is invalid.
            # This scenario should ideally be covered by `start >= total_size` for most cases.
            # However, consider range "0-10" for a 0-byte file. start=0, total_size=0. `start >= total_size` -> 416. Correct.
            if end_inclusive < start and total_size > 0 : # total_size > 0 condition to allow 0- (-1) for 0 byte file
                # print(f"DEBUG: parse_range_header: After capping end byte ({end_inclusive}), it's less than start byte ({start}). Raising 416.")
                raise HTTPException(status_code=416, detail="Range invalid after adjusting to content length (end < start).")


    else: # e.g., "bytes=100-" (from start to end of file)
        if total_size is None:
            # If total size is unknown, "bytes=N-" implies streaming until EOF.
            # For header construction (Content-Range), this is problematic.
            # The caller (stream_video endpoint) must handle this.
            # parse_range_header's role is to return a concrete start and end if possible for that header.
            # If it can't, it should indicate this. Returning (start, None) was one way.
            # However, your new stream_video logic expects a concrete end for Content-Range.
            # Let's adhere to the function signature: tuple[int, int].
            # We must raise an error if we can't determine a concrete end for "N-".
            # print(f"DEBUG: parse_range_header: Range 'bytes=N-' with unknown total_size. Cannot determine concrete end. Raising 400 or 416.")
            # A 400 might be more appropriate as the request is malformed *in the context of requiring a concrete range*.
            # Or, the server could choose to not support "N-" when total_size is unknown for ranged responses.
            # Given the usage for Content-Range, a 416 might be if it implies "cannot satisfy this precise request".
            # Let's use 400 as it's more about the request lacking info (total_size) to be fully processed for a specific Content-Range.
            raise HTTPException(status_code=400, detail="Range 'bytes=N-' requires a known total file size to determine the end boundary for Content-Range header when total size is not known.")

        # total_size is known.
        # The `start >= total_size` check above already handled invalid start.
        # So, here, start < total_size.
        end_inclusive = total_size - 1
        # Handle 0-byte file case: "bytes=0-", total_size=0. start=0. end_inclusive = -1.
        # This is a convention for "empty range".

    # Final check for 0-byte file, ensuring end_inclusive is -1 if start is 0.
    if total_size == 0:
        if start == 0:
            end_inclusive = -1 # Represents an empty range at the start of a 0-byte file.
        else: # start > 0 for a 0-byte file, already handled by `start >= total_size` -> 416
            pass


    # print(f"DEBUG: parse_range_header: Parsed: start={start}, end_inclusive={end_inclusive} (total_size={total_size})")
    return start, end_inclusive

# The FastAPI app and endpoint definitions will be in app/api/video.py or app/main.py
# This file should focus on the YTDLPSeekableStream class and related helpers.
