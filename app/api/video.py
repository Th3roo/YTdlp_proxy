import logging
from fastapi import APIRouter, HTTPException, Body, BackgroundTasks, Request, Header
from typing import List, Optional, Dict, Annotated
import uuid
import os
import asyncio
from fastapi.responses import StreamingResponse, Response
import yt_dlp

from app.models.video import (
    VideoCreate,
    VideoInQueue,
    AddVideoResponse,
    QueueResponse,
    CurrentVideoResponse,
    ActionSuccessResponse,
    ErrorResponse,
)
from app.core.ytdlp import get_video_info, download_video
from app.streaming import (
    YTDLPSeekableStream,
    parse_range_header,
)
from app.config import YDL_OPTS
from app.queue_manager import VideoQueueManager

router = APIRouter()
queue_manager = VideoQueueManager()
logger = logging.getLogger(__name__)

PLACEHOLDER_VIDEO_PATH = "static/offline_video.mp4"


async def read_file_chunked(
    file_path: str, start: int, end_inclusive: int, chunk_size: int = 64 * 1024
):
    """Reads and yields a part of a file asynchronously. end_inclusive IS INCLUSIVE."""
    try:
        loop = asyncio.get_event_loop()
        file_size = (await loop.run_in_executor(None, os.stat, file_path)).st_size

        if start < 0:
            start = 0

        if file_size == 0:
            if start == 0 and end_inclusive == -1:
                yield b""
                return
            else:
                yield b""
                return

        if start >= file_size:
            yield b""
            return

        actual_end_inclusive = min(end_inclusive, file_size - 1)
        bytes_to_read = actual_end_inclusive - start + 1

        if bytes_to_read <= 0:
            yield b""
            return

        f = await loop.run_in_executor(None, open, file_path, "rb")
        try:
            await loop.run_in_executor(None, f.seek, start)
            remaining_bytes = bytes_to_read
            while remaining_bytes > 0:
                read_amount = min(chunk_size, remaining_bytes)
                chunk = await loop.run_in_executor(None, f.read, read_amount)
                if not chunk:
                    break
                yield chunk
                remaining_bytes -= len(chunk)
        finally:
            if f:
                await loop.run_in_executor(None, f.close)

    except FileNotFoundError as e_fnf:
        logger.error(
            f"Error in read_file_chunked: Placeholder file not found at {file_path}. Error: {e_fnf}",
            exc_info=True)
        raise RuntimeError(
            f"Streaming failed: file {file_path} not found during read_file_chunked operation."
        ) from e_fnf
    except Exception as e:
        logger.error(
            f"Error streaming placeholder file {file_path} during read_file_chunked operation: {e}",
            exc_info=True)
        raise RuntimeError(
            f"Streaming failed for file {file_path} during read_file_chunked operation."
        ) from e


ext_to_mime = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "flv": "video/x-flv",
    "mov": "video/quicktime",
    "m4a": "audio/mp4",
}


@router.get("/live_stream")
async def stream_live_video(
    request: Request, range_header: Optional[str] = Header(None, alias="Range")
):
    active_video_url, active_video_title = (
        queue_manager.get_current_video_details_for_stream()
    )
    loop = asyncio.get_event_loop()
    stream = None

    if active_video_url:
        try:
            stream_ydl_opts = YDL_OPTS.copy()
            stream = YTDLPSeekableStream(
                url=active_video_url, ydl_opts=stream_ydl_opts, loop=loop
            )

            total_size = stream.total_size
            content_type = "video/mp4"
            if stream.format:
                mime = (
                    stream.format.get("mime_type")
                    or stream.format.get("mimetype")
                    or (
                        ext_to_mime.get(stream.format["ext"])
                        if "ext" in stream.format
                        else None
                    )
                )
                if mime:
                    content_type = mime

            start_byte = 0
            end_byte_inclusive = (total_size - 1) if total_size is not None else None
            status_code = 200
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Type": content_type,
                "X-Stream-Title": (
                    active_video_title.encode("utf-8", "surrogateescape").decode(
                        "latin-1", "replace"
                    )
                    if active_video_title
                    else "Unknown"
                ),
            }
            response_content_length = total_size

            if range_header and total_size is not None:
                try:
                    start_byte, end_byte_inclusive = parse_range_header(
                        range_header, total_size
                    )
                    await stream.seek(start_byte)
                    response_content_length = end_byte_inclusive - start_byte + 1
                    if response_content_length < 0:
                        response_content_length = 0
                    headers["Content-Range"] = (
                        f"bytes {start_byte}-{end_byte_inclusive}/{total_size}"
                    )
                    status_code = 206
                except HTTPException as e_range:
                    if stream:
                        await stream.close()
                    raise e_range

            if response_content_length is not None:
                headers["Content-Length"] = str(response_content_length)

            async def content_iterator():
                bytes_yielded_for_this_response = 0
                try:
                    while True:
                        chunk_read_size = 64 * 1024
                        if response_content_length is not None:
                            remaining_for_response = (
                                response_content_length
                                - bytes_yielded_for_this_response
                            )
                            if remaining_for_response <= 0:
                                break
                            chunk_read_size = min(
                                chunk_read_size, remaining_for_response
                            )
                        if chunk_read_size <= 0:
                            break
                        chunk = await stream.read(chunk_read_size)
                        if not chunk:
                            break
                        yield chunk
                        bytes_yielded_for_this_response += len(chunk)
                except Exception as e_iter_live:
                    logger.error(
                        f"Error during /live_stream content iteration for {active_video_url}: {e_iter_live}",
                        exc_info=True)
                finally:
                    if stream:
                        await stream.close()

            return StreamingResponse(
                content_iterator(), status_code=status_code, headers=headers
            )

        except HTTPException as e_http_setup:
            if stream:
                await stream.close()
            if e_http_setup.status_code in [400, 404, 500, 502, 503]:
                active_video_url = None
            else:
                raise e_http_setup
        except Exception as e_general_setup:
            if stream:
                await stream.close()
            logger.error(
                f"Unexpected error during /live_stream setup for {active_video_url}: {e_general_setup}",
                exc_info=True)
            active_video_url = None

    if not active_video_url:
        if not await loop.run_in_executor(None, os.path.exists, PLACEHOLDER_VIDEO_PATH):
            raise HTTPException(
                status_code=500, detail="Placeholder video file not found on server."
            )
        try:
            file_size = (
                await loop.run_in_executor(None, os.stat, PLACEHOLDER_VIDEO_PATH)
            ).st_size
        except FileNotFoundError:
            raise HTTPException(
                status_code=500, detail="Placeholder disappeared before stat."
            )

        ph_status_code = 200
        ph_headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": "video/mp4",
            "X-Stream-Title": "Stream Offline",
        }
        ph_start_byte, ph_end_byte_inclusive = 0, file_size - 1 if file_size > 0 else -1
        ph_response_content_length = file_size

        if range_header:
            try:
                ph_start_byte, ph_end_byte_inclusive = parse_range_header(
                    range_header, file_size
                )
                ph_response_content_length = ph_end_byte_inclusive - ph_start_byte + 1
                if ph_response_content_length < 0:
                    ph_response_content_length = 0
                ph_headers["Content-Range"] = (
                    f"bytes {ph_start_byte}-{ph_end_byte_inclusive}/{file_size}"
                )
                ph_status_code = 206
            except HTTPException as e_ph_range:
                raise e_ph_range

        ph_headers["Content-Length"] = str(ph_response_content_length)
        return StreamingResponse(
            read_file_chunked(
                PLACEHOLDER_VIDEO_PATH, ph_start_byte, ph_end_byte_inclusive
            ),
            status_code=ph_status_code,
            headers=ph_headers,
        )


@router.get("/stream/{video_id}")
async def stream_video(
    video_id: str,
    request: Request,
    range: Annotated[str | None, Header()] = None,
):
    url = f"https://www.youtube.com/watch?v={video_id}"
    stream = None
    try:
        ydl_opts_for_stream = YDL_OPTS.copy()
        stream = YTDLPSeekableStream(url, ydl_opts=ydl_opts_for_stream)
        total_size = stream.total_size
        content_type = "video/mp4"
        if stream.format:
            mime = (
                stream.format.get("mime_type")
                or stream.format.get("mimetype")
                or (
                    ext_to_mime.get(stream.format["ext"])
                    if "ext" in stream.format
                    else None
                )
            )
            if mime:
                content_type = mime

        start_byte = 0
        end_byte_inclusive = (total_size - 1) if total_size is not None else None
        status_code = 200
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": content_type,
        }
        response_content_length = total_size

        if range and total_size is not None:
            try:
                start_byte, end_byte_inclusive = parse_range_header(range, total_size)
                await stream.seek(start_byte)
                response_content_length = end_byte_inclusive - start_byte + 1
                if response_content_length < 0:
                    response_content_length = 0
                headers["Content-Range"] = (
                    f"bytes {start_byte}-{end_byte_inclusive}/{total_size}"
                )
                status_code = 206
            except HTTPException as e_range_direct:
                if stream:
                    await stream.close()
                raise e_range_direct

        if response_content_length is not None:
            headers["Content-Length"] = str(response_content_length)

        async def iter_content():
            bytes_yielded = 0
            chunk_size = 64 * 1024
            try:
                while True:
                    read_this_chunk = chunk_size
                    if response_content_length is not None:
                        remaining_for_this_response = (
                            response_content_length - bytes_yielded
                        )
                        if remaining_for_this_response <= 0:
                            break
                        read_this_chunk = min(
                            read_this_chunk, remaining_for_this_response
                        )
                    if read_this_chunk <= 0:
                        break
                    data = await stream.read(read_this_chunk)
                    if not data:
                        break
                    yield data
                    bytes_yielded += len(data)
            except Exception as e_iter_direct:
                logger.error(
                    f"Error in /stream/{video_id} content iterator for {url}: {e_iter_direct}",
                    exc_info=True)
            finally:
                if stream:
                    await stream.close()

        return StreamingResponse(
            iter_content(),
            status_code=status_code,
            headers=headers,
            media_type=content_type,
        )

    except yt_dlp.utils.DownloadError as e_yt_dlp_direct:
        if stream:
            await stream.close()
        if "video unavailable" in str(e_yt_dlp_direct).lower():
            raise HTTPException(
                status_code=404,
                detail=f"Video {video_id} is unavailable: {e_yt_dlp_direct}",
            )
        raise HTTPException(
            status_code=502,
            detail=f"yt-dlp download error for {video_id}: {e_yt_dlp_direct}",
        )
    except HTTPException as e_http_direct:
        if stream:
            await stream.close()
        raise e_http_direct
    except Exception as e_general_direct:
        if stream:
            await stream.close()
        logger.error(
            f"Stream for {video_id}: An unexpected error occurred: {e_general_direct}",
            exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"An unexpected error occurred while trying to stream {video_id}: {type(e_general_direct).__name__}",
        )


@router.post("/video/add", response_model=AddVideoResponse, status_code=202)
async def add_video_to_queue(
    payload: VideoCreate = Body(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    """Adds a video to the queue by URL and starts a background task to get metadata."""
    new_video_entry, position = queue_manager.add_video(payload, background_tasks)
    return AddVideoResponse(
        message="Video added to queue, fetching metadata.",
        video_info=new_video_entry,
        queue_position=position,
    )


@router.get("/queue", response_model=QueueResponse)
async def get_queue_state():
    """Returns the current state of the video queue."""
    queue_list, current_id, total_items = queue_manager.get_queue_state()
    return QueueResponse(
        queue=queue_list, current_video_id_in_queue=current_id, total_items=total_items
    )


@router.post(
    "/video/play_next",
    response_model=ActionSuccessResponse,
    responses={404: {"model": ErrorResponse}},
)
async def play_next_video():
    """Switches to the next video in the queue."""
    current_video = queue_manager.play_next_video()
    return ActionSuccessResponse(
        message="Playing next video", current_video=current_video
    )


@router.post(
    "/video/play_previous",
    response_model=ActionSuccessResponse,
    responses={404: {"model": ErrorResponse}},
)
async def play_previous_video():
    """Switches to the previous video in the queue."""
    current_video = queue_manager.play_previous_video()
    return ActionSuccessResponse(
        message="Playing previous video", current_video=current_video
    )


@router.post(
    "/video/pause_resume",
    response_model=ActionSuccessResponse,
    responses={404: {"model": ErrorResponse}},
)
async def pause_resume_video():
    """Pauses/resumes the current video (simulated)."""
    current_video_id = queue_manager.current_video_id_in_queue
    if not current_video_id:
        raise HTTPException(
            status_code=404, detail="No video is currently active or selected"
        )
    current_video = queue_manager.get_video_entry(current_video_id)
    if not current_video:
        raise HTTPException(status_code=404, detail="Current video not found in store.")
    action = "resumed" if getattr(current_video, "is_paused", False) else "paused"
    return ActionSuccessResponse(
        message=f"Video '{current_video.title}' {action} (simulated).",
        current_video=current_video,
    )


@router.get(
    "/current_video_link",
    response_model=CurrentVideoResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_current_video_details():
    """Returns information about the currently active video."""
    current_video = queue_manager.get_current_video_info_api()
    if current_video:
        return CurrentVideoResponse(
            message="Current active video details.", video_info=current_video
        )
    else:
        return CurrentVideoResponse(
            message="Video queue is empty or no video selected.", video_info=None
        )


@router.post(
    "/video/{video_id_in_queue}/cancel_download",
    response_model=ActionSuccessResponse,
    responses={404: {"model": ErrorResponse}},
)
async def cancel_video_download(video_id_in_queue: str):
    """Cancels video download (sets status)."""
    updated_video_entry = queue_manager.cancel_download(video_id_in_queue)
    message = f"Download for '{updated_video_entry.title}' "
    if updated_video_entry.status == "metadata_fetched":
        message += "has been marked as cancelled."
    elif updated_video_entry.status == "downloaded":
        message = (
            f"Video '{updated_video_entry.title}' is already downloaded. Cannot cancel."
        )
    else:
        message = f"Video '{updated_video_entry.title}' is not currently downloading (status: {updated_video_entry.status}). No action taken."
    return ActionSuccessResponse(message=message, current_video=updated_video_entry)


@router.post(
    "/video/{video_id_in_queue}/download",
    response_model=ActionSuccessResponse,
    status_code=202,
    responses={404: {"model": ErrorResponse}},
)
async def download_single_video(
    video_id_in_queue: str, background_tasks: BackgroundTasks
):
    """Initiates download of the specified video from the queue."""
    updated_video_entry = queue_manager.initiate_download(
        video_id_in_queue, background_tasks
    )
    message = f"Download initiated for video '{updated_video_entry.title}'."
    if updated_video_entry.status == "downloaded":
        message = f"Video '{updated_video_entry.title}' is already downloaded."
    elif updated_video_entry.status == "downloading":
        message = f"Video '{updated_video_entry.title}' is already downloading."
    return ActionSuccessResponse(message=message, current_video=updated_video_entry)
