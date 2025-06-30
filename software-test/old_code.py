import io
import os
import re
import time
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request, Header, Response
from fastapi.responses import StreamingResponse
import yt_dlp

app = FastAPI()


class YTDLPSeekableStream:

    def __init__(self, url: str, ydl_opts: dict = None):
        self.url = url
        self.ydl_opts = ydl_opts or {}
        self.ydl = yt_dlp.YoutubeDL(self.ydl_opts)
        # Initialize filepath *immediately*
        temp_filename = self.ydl.prepare_filename(
            {"title": "temp", "id": "temp"}
        )  # Dummy info_dict
        self.filepath = temp_filename + ".ytdlp"
        self.info_dict = self.ydl.extract_info(url, download=False)
        if not self.info_dict:
            raise HTTPException(status_code=404, detail="Video not found")

        self.format = self._get_selected_format()
        if not self.format:
            raise HTTPException(status_code=400, detail="No suitable format found.")
        # Update filepath with the *correct* filename
        self.filepath = self.ydl.prepare_filename(self.info_dict) + ".ytdlp"

        self._current_pos = 0
        self.total_size = self.format.get("filesize") or self.format.get(
            "filesize_approx"
        )
        self._file = None

    def _get_selected_format(self):
        formats = self.info_dict.get("formats", [])
        if not formats:
            return None
        requested_format = self.ydl_opts.get(
            "format", "bestvideo+bestaudio/best"
        )  # default value
        for fmt in formats:
            if fmt.get("format_id") == requested_format:
                return fmt
        try:
            processed_info = self.ydl.process_video_result(
                self.info_dict, download=False
            )
            selected_format = processed_info.get("requested_formats")
            if selected_format:
                return selected_format[0]
            elif "format_id" in processed_info:
                return processed_info
            else:
                return None
        except (yt_dlp.utils.YtDlpError, KeyError):
            return None

    def _ensure_downloaded(self, start_byte: int, end_byte: int):
        max_retries = 5
        retry_delay = 1  # Initial delay in seconds

        for attempt in range(max_retries):
            try:
                ydl_opts = self.ydl_opts.copy()
                ydl_opts.update({
                    "format": self.format["format_id"],
                    "outtmpl": self.filepath,
                    "continuedl": True,
                    "http_headers": {"Range": f"bytes={start_byte}-{end_byte}"},
                    # 'nocheckcertificate': True,  # Use with extreme caution!
                })
                with yt_dlp.YoutubeDL(ydl_opts) as ydl_segment:
                    ydl_segment.download([self.url])
                return  # Success! Exit the retry loop

            except yt_dlp.utils.DownloadError as e:
                if "HTTP Error 416" in str(e):
                    raise HTTPException(
                        status_code=416, detail="Requested Range Not Satisfiable"
                    )
                elif (
                    "EOF occurred in violation of protocol" in str(e)
                    or "Connection reset by peer" in str(e)
                ):
                    # Potentially transient errors, retry
                    if attempt == max_retries - 1:  # Last attempt
                        raise HTTPException(
                            status_code=500,
                            detail=f"yt-dlp download error after retries: {e}",
                        )
                    print(
                        f"Download error (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {retry_delay} seconds..."
                    )
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    # Other errors, don't retry
                    raise HTTPException(
                        status_code=500, detail=f"yt-dlp download error: {e}"
                    ) from e

            except Exception as e:  # catch other exceptions
                raise HTTPException(
                    status_code=500, detail=f"An unexpected error occurred: {e}"
                ) from e

    def _open_file(self):
        if self._file is None:
            # No need to check os.path.exists() here, _ensure_downloaded() handles it
            self._ensure_downloaded(
                0,
                (self.total_size - 1)
                if self.total_size
                else 1024 * 1024,  # download at least 1mb, to get data
            )
            self._file = open(self.filepath, "rb")
        return self._file

    def read(self, size: int = -1) -> bytes:
        # open file
        file = self._open_file()  # This will now trigger the initial download if needed.
        if size == 0:
            return b""

        # If size is -1, and we know total size, we can set an end range.
        if size == -1 and self.total_size:
            target_end = self.total_size - 1
        # if we don't have total size, use 10 mb
        elif size == -1:
            target_end = self._current_pos + 10 * 1024 * 1024
        # if we have size
        else:
            target_end = self._current_pos + size - 1

        #_ensure_download if we have not enough data
        current_file_size = os.path.getsize(self.filepath)
        if current_file_size - 1 < target_end:
            self._ensure_downloaded(self._current_pos, target_end)

        file.seek(self._current_pos)  # seek
        data = file.read(size)
        self._current_pos += len(data)
        return data

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            new_pos = offset
        elif whence == io.SEEK_CUR:
            new_pos = self._current_pos + offset
        elif whence == io.SEEK_END:
            if self.total_size is None:
                raise ValueError(
                    "SEEK_END is not supported for unknown file size."
                )
            new_pos = self.total_size + offset
        else:
            raise ValueError(
                "Invalid whence value. Use io.SEEK_SET, io.SEEK_CUR, or io.SEEK_END."
            )

        if new_pos < 0:
            new_pos = 0

        self._current_pos = new_pos
        return self._current_pos

    def tell(self) -> int:
        return self._current_pos

    def close(self):
        if self._file:
            self._file.close()
            self._file = None
        if self.filepath and os.path.exists(self.filepath):
            os.remove(self.filepath)


async def get_video_stream(url: str, range_header: str | None = None):
    print()
    ydl_opts = {
        "quiet": True,
        "noprogress": True,
        'cookiesfrombrowser': ('firefox',),
        # 'nocheckcertificate': True,  # Use with extreme caution!
    }

    stream = YTDLPSeekableStream(url, ydl_opts)
    total_size = stream.total_size
    start = 0
    end = total_size - 1 if total_size else None

    if range_header:
        try:
            start, end = parse_range_header(range_header, total_size)
            stream.seek(start)  # seek to start of range
        except ValueError as e:
            raise HTTPException(status_code=416, detail=str(e))
    return stream, start, end, total_size


def parse_range_header(
    range_header: str, total_size: int | None
) -> tuple[int, int | None]:
    match = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not match:
        raise ValueError("Invalid Range header format")

    start = int(match.group(1))
    end_str = match.group(2)

    if end_str:
        end = int(end_str) + 1
    elif total_size is not None:
        end = total_size
    else:
        end = None
    if total_size is not None and start >= total_size:
        raise ValueError("Start range exceeds content length")
    if end is not None and start >= end:
        raise ValueError("Invalid range: start must be less than end")
    return start, end


@app.get("/stream/{video_id}")
async def stream_video(
    video_id: str,
    request: Request,
    range: Annotated[str | None, Header()] = None,
):
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        stream, start, end, total_size = await get_video_stream(url, range)

        async def iter_content():
            chunk_size = 64 * 1024
            try:
                while True:
                    data = stream.read(chunk_size)
                    if not data:
                        break

                    # *** KEY CHANGE: Calculate headers *inside* the generator, ***
                    # *** *after* the read() call that triggers the download. ***
                    if range:  # Range request
                        status_code = 206
                        content_length = len(data)  # Actual length of *this* chunk
                        current_end = stream.tell()
                        headers = {
                            "Accept-Ranges": "bytes",
                            "Content-Range": f"bytes {current_end - content_length}-{current_end - 1}/{total_size}",
                            "Content-Length": str(content_length),  # Length of *this* chunk
                        }
                    else:  # NOT a Range request
                        status_code = 200
                        headers = {}
                        # Only set Content-Length if total_size is known.
                        if total_size is not None:
                            headers["Content-Length"] = str(total_size)

                    # Send headers *with each chunk* for range requests.
                    # This is necessary because the download might happen
                    # incrementally.  For non-range requests, we send
                    # headers only once, at the beginning.
                    if range or not hasattr(request.state, "headers_sent"):
                        # Use request.state to track if headers have been sent.
                        request.state.headers_sent = True
                        yield Response(
                            content=b"",  # Empty content, just headers
                            status_code=status_code,
                            headers=headers,
                            media_type="video/mp4",
                        ).body  # Get the encoded headers

                    yield data
            except Exception as e:
                print(f"Error in stream generator: {e}")  # log error
            finally:
                stream.close()  # close stream

        return StreamingResponse(
            iter_content(), media_type="video/mp4"
        )  # No headers

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp download error: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:  # catch http exceptions
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"An unexpected error occurred: {e}"
        )