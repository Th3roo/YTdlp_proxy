from fastapi import APIRouter, HTTPException, Body, BackgroundTasks
from typing import List, Optional, Dict
import uuid # Для генерации уникальных ID для видео в очереди

# Импортируем модели и функции ytdlp
from app.models.video import (
    VideoCreate, VideoInQueue, AddVideoResponse, QueueResponse,
    CurrentVideoResponse, ActionSuccessResponse, ErrorResponse, VideoInQueue
)
from app.core.ytdlp import get_video_info, download_video
from app.streaming import YTDLPSeekableStream, parse_range_header # parse_range_header is used by new stream endpoint
from app.config import YDL_OPTS # Import consolidated YDL_OPTS
from app.queue_manager import VideoQueueManager # Import the new manager

import os
import stat # Keep for placeholder
import asyncio
from fastapi.responses import StreamingResponse, Response # Ensure Response is imported
# Removed 'สถานะ', assuming it was a typo. If 'status' from fastapi is needed, it can be added.
from fastapi import Request, Header, HTTPException, Depends
from typing import Annotated # For the new endpoint
import yt_dlp # Added for direct exception handling

router = APIRouter()

# Instantiate the VideoQueueManager
# This will hold the queue state and logic.
queue_manager = VideoQueueManager()

# --- Путь к файлу-заглушке ---
PLACEHOLDER_VIDEO_PATH = "static/offline_video.mp4" # Убедитесь, что этот файл существует

# --- Вспомогательная функция для стриминга локального файла (для заглушки) ---
async def read_file_chunked(file_path: str, start: int, end_inclusive: int, chunk_size: int = 64 * 1024): # end renamed to end_inclusive
    """Читает и отдает часть файла асинхронно. end_inclusive IS INCLUSIVE."""
    try:
        loop = asyncio.get_event_loop()
        file_size = (await loop.run_in_executor(None, os.stat, file_path)).st_size

        if start < 0: start = 0 # Ensure start is not negative

        # Validate start and end_inclusive relative to file_size
        if file_size == 0: # Handle 0-byte file
            if start == 0 and end_inclusive == -1: # Request for the entirety of a 0-byte file
                yield b""
                return
            else: # Any other range for a 0-byte file is invalid or yields nothing
                # print(f"Placeholder stream: Invalid range {start}-{end_inclusive} for 0-byte file. Yielding empty.")
                yield b""
                return

        # For non-empty files:
        if start >= file_size:
            # print(f"Placeholder stream: Start {start} is at or beyond file size {file_size}. Yielding empty.")
            yield b"" # Ничего не отдаем, если старт за пределами файла
            return

        # Cap end_inclusive
        actual_end_inclusive = min(end_inclusive, file_size - 1)

        bytes_to_read = actual_end_inclusive - start + 1

        if bytes_to_read <= 0:
            # print(f"Placeholder stream: No bytes to read for range {start}-{actual_end_inclusive}. Yielding empty.")
            yield b""
            return

        # print(f"Placeholder stream: Reading {bytes_to_read} bytes from {start} to {actual_end_inclusive} of {file_path}")
        f = await loop.run_in_executor(None, open, file_path, "rb")
        try:
            await loop.run_in_executor(None, f.seek, start)
            remaining_bytes = bytes_to_read
            while remaining_bytes > 0:
                read_amount = min(chunk_size, remaining_bytes)
                chunk = await loop.run_in_executor(None, f.read, read_amount)
                if not chunk:
                    # print(f"Warning: EOF reached prematurely in read_file_chunked for {file_path}. Expected {remaining_bytes} more bytes, got 0 from read.")
                    break
                yield chunk
                remaining_bytes -= len(chunk)
        finally:
            if f:
                await loop.run_in_executor(None, f.close)

    except FileNotFoundError as e_fnf:
        print(f"Error in read_file_chunked: Placeholder file not found at {file_path}. Error: {e_fnf}")
        # Raising an error is generally better than yielding b"" if Content-Length was already set,
        # as it allows higher-level error handling or results in a clearer broken stream
        # rather than a content mismatch.
        raise RuntimeError(f"Streaming failed: file {file_path} not found during read_file_chunked operation.") from e_fnf
    except Exception as e:
        print(f"Error streaming placeholder file {file_path} during read_file_chunked operation: {e}")
        # Similar to FileNotFoundError, raise to indicate failure.
        raise RuntimeError(f"Streaming failed for file {file_path} during read_file_chunked operation.") from e

# Убираем дублирование глобальных переменных, они уже объявлены выше при импорте VideoInQueue
# video_queue_store: Dict[str, VideoInQueue] = {}
# ordered_queue_ids: List[str] = []
# current_video_id_in_queue: Optional[str] = None

# Helper for mime types, can be expanded
ext_to_mime = {'mp4': 'video/mp4', 'webm': 'video/webm', 'mkv': 'video/x-matroska', 'flv': 'video/x-flv', 'mov': 'video/quicktime', 'm4a': 'audio/mp4'}

# --- Эндпоинт для стриминга ---
# Existing /live_stream endpoint is updated to use the new YTDLPSeekableStream
@router.get("/live_stream")
async def stream_live_video(request: Request, range_header: Optional[str] = Header(None, alias="Range")):
    active_video_url, active_video_title = queue_manager.get_current_video_details_for_stream()
    loop = asyncio.get_event_loop()
    stream = None # Initialize stream to None

    if active_video_url:
        try:
            # print(f"Live stream: Attempting to stream from URL: {active_video_url}")
            stream_ydl_opts = YDL_OPTS.copy()
            # Example: force a specific format for live_stream if desired, otherwise uses YTDLPSeekableStream defaults
            # stream_ydl_opts['format'] = 'best[height<=720][ext=mp4]/best[ext=mp4]'
            stream = YTDLPSeekableStream(url=active_video_url, ydl_opts=stream_ydl_opts, loop=loop)

            total_size = stream.total_size
            content_type = "video/mp4" # Default
            if stream.format:
                mime = stream.format.get('mime_type') or stream.format.get('mimetype') or \
                       (ext_to_mime.get(stream.format['ext']) if 'ext' in stream.format else None)
                if mime: content_type = mime

            start_byte = 0
            # end_byte_inclusive is the last byte index of the content (e.g. total_size-1)
            end_byte_inclusive = (total_size - 1) if total_size is not None else None

            status_code = 200
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Type": content_type,
                "X-Stream-Title": active_video_title.encode('utf-8', 'surrogateescape').decode('latin-1', 'replace') if active_video_title else "Unknown"
            }

            # This is the length of the content *being sent in this specific HTTP response*
            response_content_length = total_size

            if range_header and total_size is not None:
                try:
                    # parse_range_header returns (start_byte, end_byte_inclusive), raises 416 if error
                    start_byte, end_byte_inclusive = parse_range_header(range_header, total_size)
                    await stream.seek(start_byte) # Important: seek the stream

                    response_content_length = (end_byte_inclusive - start_byte + 1)
                    if response_content_length < 0: response_content_length = 0 # Should not happen with valid range

                    headers["Content-Range"] = f"bytes {start_byte}-{end_byte_inclusive}/{total_size}"
                    status_code = 206 # Partial Content
                except HTTPException as e_range: # Catch 416 or 400 from parse_range_header
                    if stream: await stream.close()
                    raise e_range # Propagate to client

            if response_content_length is not None:
                headers["Content-Length"] = str(response_content_length)
            # If total_size (and thus response_content_length for full request) is None, Content-Length is omitted.

            async def content_iterator():
                bytes_yielded_for_this_response = 0
                try:
                    while True:
                        # Determine how much to read in this chunk for this response
                        chunk_read_size = 64 * 1024 # Standard chunk size

                        if response_content_length is not None: # If we know the total for this response
                            remaining_for_response = response_content_length - bytes_yielded_for_this_response
                            if remaining_for_response <= 0:
                                break # Sent enough for this response
                            chunk_read_size = min(chunk_read_size, remaining_for_response)

                        if chunk_read_size <= 0 : # Safety, should be caught by loop condition
                            break

                        chunk = await stream.read(chunk_read_size)
                        if not chunk:
                            # print(f"Live stream: End of source stream for {active_video_url}. Yielded {bytes_yielded_for_this_response} bytes.")
                            break

                        yield chunk
                        bytes_yielded_for_this_response += len(chunk)
                except Exception as e_iter_live:
                    print(f"Error during /live_stream content iteration for {active_video_url}: {e_iter_live}")
                finally:
                    if stream:
                        # print(f"Closing stream in /live_stream finally for {active_video_url}")
                        await stream.close()

            return StreamingResponse(content_iterator(), status_code=status_code, headers=headers)

        except HTTPException as e_http_setup: # From YTDLPSeekableStream setup or parse_range_header
            if stream: await stream.close()
            # print(f"/live_stream setup HTTPException for {active_video_url}: {e_http_setup.detail}")
            # If the error is something like 404 (video not found by ytdlp) or 400/50x (bad setup/yt-dlp internal error)
            # then fallback to placeholder. A 416 should have been re-raised by range parsing block.
            if e_http_setup.status_code in [400, 404, 500, 502, 503]:
                active_video_url = None # Signal to use placeholder
            else: # Re-raise other specific HTTP errors like 416 if they somehow reach here
                raise e_http_setup
        except Exception as e_general_setup: # Any other unexpected error during setup
            if stream: await stream.close()
            print(f"Unexpected error during /live_stream setup for {active_video_url}: {e_general_setup}")
            active_video_url = None # Fallback to placeholder

    # Fallback to placeholder if active_video_url is None
    if not active_video_url:
        # print(f"/live_stream: Falling back to placeholder {PLACEHOLDER_VIDEO_PATH}")
        if not await loop.run_in_executor(None, os.path.exists, PLACEHOLDER_VIDEO_PATH):
            raise HTTPException(status_code=500, detail="Placeholder video file not found on server.")

        try:
            file_size = (await loop.run_in_executor(None, os.stat, PLACEHOLDER_VIDEO_PATH)).st_size
        except FileNotFoundError: # Should be caught by exists check, but defensive
             raise HTTPException(status_code=500, detail="Placeholder disappeared before stat.")

        ph_status_code = 200
        ph_headers = {"Accept-Ranges": "bytes", "Content-Type": "video/mp4", "X-Stream-Title": "Stream Offline"}
        ph_start_byte, ph_end_byte_inclusive = 0, file_size - 1 if file_size > 0 else -1
        ph_response_content_length = file_size

        if range_header:
            try:
                ph_start_byte, ph_end_byte_inclusive = parse_range_header(range_header, file_size)
                ph_response_content_length = (ph_end_byte_inclusive - ph_start_byte + 1)
                if ph_response_content_length < 0: ph_response_content_length = 0
                ph_headers["Content-Range"] = f"bytes {ph_start_byte}-{ph_end_byte_inclusive}/{file_size}"
                ph_status_code = 206
            except HTTPException as e_ph_range: # e.g. 416
                raise e_ph_range

        ph_headers["Content-Length"] = str(ph_response_content_length)
        return StreamingResponse(
            read_file_chunked(PLACEHOLDER_VIDEO_PATH, ph_start_byte, ph_end_byte_inclusive),
            status_code=ph_status_code, headers=ph_headers
        )

# --- Новый эндпоинт для стриминга по video_id ---
@router.get("/stream/{video_id}")
async def stream_video(
    video_id: str,
    request: Request, # Added request for state
    range: Annotated[str | None, Header()] = None, # Corrected alias to 'range'
):
    url = f"https://www.youtube.com/watch?v={video_id}"
    stream = None # Initialize stream
    # print(f"Attempting to stream video_id: {video_id} from URL: {url}")

    try:
        # Using YDL_OPTS from app.config by default for YTDLPSeekableStream
        # Specific format selection can be part of ydl_opts if needed, e.g. {'format': 'best'}
        ydl_opts_for_stream = YDL_OPTS.copy()
        # ydl_opts_for_stream['debug_printtraffic'] = True # Enable for debugging if necessary
        # ydl_opts_for_stream['format'] = 'bv*+ba/b' # Example: best video and audio, then best overall

        stream = YTDLPSeekableStream(url, ydl_opts=ydl_opts_for_stream) # Loop will be fetched by YTDLPSeekableStream constructor

        total_size = stream.total_size
        # print(f"Stream for {video_id}: Total size: {total_size}")

        # Determine Content-Type
        content_type = "video/mp4" # Default
        if stream.format:
            mime = stream.format.get('mime_type') or stream.format.get('mimetype') or \
                   (ext_to_mime.get(stream.format['ext']) if 'ext' in stream.format else None)
            if mime: content_type = mime

        start_byte = 0
        # end_byte_inclusive is the last byte of the content to send
        end_byte_inclusive = (total_size - 1) if total_size is not None else None

        status_code = 200 # OK
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": content_type,
            # "X-Video-ID": video_id # Custom header if useful
        }
        # This is the length of the content *being sent in this specific HTTP response*
        response_content_length = total_size

        if range and total_size is not None: # Handle range requests if total_size is known
            try:
                # parse_range_header returns (start, inclusive_end) or raises HTTPException 416
                start_byte, end_byte_inclusive = parse_range_header(range, total_size)
                # print(f"Stream for {video_id}: Range parsed: {start_byte}-{end_byte_inclusive}/{total_size}")

                await stream.seek(start_byte) # Seek the stream to the start of the requested range

                response_content_length = (end_byte_inclusive - start_byte + 1)
                if response_content_length < 0: response_content_length = 0

                headers["Content-Range"] = f"bytes {start_byte}-{end_byte_inclusive}/{total_size}"
                status_code = 206 # Partial Content
            except HTTPException as e_range_direct: # Catch 416 or 400 from parse_range_header
                if stream: await stream.close()
                # print(f"Stream for {video_id}: Range parse error: {e_range_direct.detail}")
                raise e_range_direct # Propagate to client

        if response_content_length is not None:
            headers["Content-Length"] = str(response_content_length)
        # If total_size is None and no range, Content-Length is omitted.

        # print(f"Stream for {video_id}: Responding with status {status_code}, headers: {headers}")

        async def iter_content():
            # This generator yields chunks of the video stream.
            # It respects response_content_length to send the correct amount of data.
            bytes_yielded = 0
            chunk_size = 64 * 1024 # 64KB chunks

            # The 'request.state.headers_sent' logic from your provided code for /stream/{video_id}
            # seems to be for a very specific way of sending headers *with each chunk*.
            # This is not standard for StreamingResponse in FastAPI when headers are passed to StreamingResponse constructor.
            # FastAPI sends headers once before starting to stream the body.
            # The YTDLPSeekableStream is designed to download progressively.
            # The initial headers (Content-Type, Content-Length for full or range) should be sufficient.
            # If the goal was to update headers mid-stream, that's more complex and not typical.
            # I will simplify the iterator to just yield data according to the calculated range/length.

            try:
                while True:
                    # Determine how much to read in this chunk
                    read_this_chunk = chunk_size
                    if response_content_length is not None: # If we know the total for this response
                        remaining_for_this_response = response_content_length - bytes_yielded
                        if remaining_for_this_response <= 0:
                            break # Sent enough for this response
                        read_this_chunk = min(read_this_chunk, remaining_for_this_response)

                    if read_this_chunk <= 0: break # Safety

                    data = await stream.read(read_this_chunk)
                    if not data:
                        # print(f"Stream for {video_id}: No more data from stream.read(). Yielded {bytes_yielded} bytes.")
                        break # End of source stream

                    yield data
                    bytes_yielded += len(data)
            except Exception as e_iter_direct:
                print(f"Error in /stream/{video_id} content iterator for {url}: {e_iter_direct}")
            finally:
                if stream:
                    # print(f"Closing stream in /stream/{video_id} finally for {url}")
                    await stream.close()

        return StreamingResponse(iter_content(), status_code=status_code, headers=headers, media_type=content_type)

    except yt_dlp.utils.DownloadError as e_yt_dlp_direct:
        if stream: await stream.close()
        # print(f"Stream for {video_id}: yt-dlp DownloadError: {e_yt_dlp_direct}")
        # Check for common unavailable errors
        if "video unavailable" in str(e_yt_dlp_direct).lower():
            raise HTTPException(status_code=404, detail=f"Video {video_id} is unavailable: {e_yt_dlp_direct}")
        raise HTTPException(status_code=502, detail=f"yt-dlp download error for {video_id}: {e_yt_dlp_direct}")
    except HTTPException as e_http_direct: # Catch HTTPExceptions from YTDLPSeekableStream or parse_range_header
        if stream: await stream.close()
        # print(f"Stream for {video_id}: HTTPException: {e_http_direct.detail}")
        raise e_http_direct # Re-raise (e.g., 404, 416, 500 from stream setup)
    except Exception as e_general_direct:
        if stream: await stream.close()
        print(f"Stream for {video_id}: An unexpected error occurred: {e_general_direct}") # Log full error
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred while trying to stream {video_id}: {type(e_general_direct).__name__}")


# --- Эндпоинты управления очередью (ОСТАЮТСЯ БЕЗ ИЗМЕНЕНИЙ В ЭТОМ ШАГЕ) ---
# Background task functions (_fetch_and_update_metadata, _initiate_download_task, _download_video_and_update_status)
# will be moved to VideoQueueManager in the next step.
# For now, their direct calls from endpoints will be modified or temporarily removed if the logic moves entirely.


@router.post("/video/add", response_model=AddVideoResponse, status_code=202) # 202 Accepted для фоновой задачи
async def add_video_to_queue(payload: VideoCreate = Body(...), background_tasks: BackgroundTasks = BackgroundTasks()):
    """
    Добавляет видео в очередь по URL и запускает фоновую задачу для получения метаданных.
    """
    new_video_entry, position = queue_manager.add_video(payload, background_tasks)

    # The actual background task for metadata fetching will be initiated by the queue_manager.
    # This will be fully implemented when moving _fetch_and_update_metadata to the manager.
    # For now, we assume add_video might trigger it or prepare for it.
    # queue_manager.schedule_metadata_fetch(new_video_entry.id_in_queue, background_tasks)
    # This line is conceptual for now. The actual call will be inside add_video or a separate call.

    # TODO: Modify VideoQueueManager.add_video to also handle the background task scheduling for metadata.
    # For now, we expect _fetch_and_update_metadata to be moved into VideoQueueManager
    # and called appropriately, likely needing `background_tasks` passed to it or
    # `get_video_info` called directly if we make `_fetch_and_update_metadata` an async method
    # within the manager.
    # Let's assume for now the manager's add_video is responsible for starting this process.
    # The background_tasks are passed to it.

    # The background task for metadata fetching will be handled by the VideoQueueManager.
    # (This will be implemented in the next step when moving _fetch_and_update_metadata)
    # For now, we'll manually add the task here, calling a method on the manager that will exist later.
    # This is a temporary measure until the background task functions are moved.
    # background_tasks.add_task(queue_manager._fetch_and_update_metadata_task, new_video_entry.id_in_queue, str(payload.url))
    # The VideoQueueManager's add_video method now handles scheduling the metadata fetch task.
    return AddVideoResponse(
        message="Video added to queue, fetching metadata.",
        video_info=new_video_entry,
        queue_position=position
    )

@router.get("/queue", response_model=QueueResponse)
async def get_queue_state():
    """
    Возвращает текущее состояние очереди видео.
    """
    queue_list, current_id, total_items = queue_manager.get_queue_state()
    return QueueResponse(
        queue=queue_list,
        current_video_id_in_queue=current_id,
        total_items=total_items
    )

@router.post("/video/play_next", response_model=ActionSuccessResponse, responses={404: {"model": ErrorResponse}})
async def play_next_video():
    """
    Переключает на следующее видео в очереди.
    """
    current_video = queue_manager.play_next_video()
    return ActionSuccessResponse(message="Playing next video", current_video=current_video)


@router.post("/video/play_previous", response_model=ActionSuccessResponse, responses={404: {"model": ErrorResponse}})
async def play_previous_video():
    """
    Переключает на предыдущее видео в очереди.
    """
    current_video = queue_manager.play_previous_video()
    return ActionSuccessResponse(message="Playing previous video", current_video=current_video)


@router.post("/video/pause_resume", response_model=ActionSuccessResponse, responses={404: {"model": ErrorResponse}})
async def pause_resume_video():
    """
    Ставит на паузу/возобновляет текущее видео.
    (Пока это заглушка, т.к. реальное управление плеером не реализовано на бэкенде)
    """
    # This logic might need more info from queue_manager or stay as is if it's purely simulative
    current_video_id = queue_manager.current_video_id_in_queue # Direct access for now, or method
    if not current_video_id:
        raise HTTPException(status_code=404, detail="No video is currently active or selected")

    current_video = queue_manager.get_video_entry(current_video_id)
    if not current_video:
        # Should not happen if current_video_id is valid
        raise HTTPException(status_code=404, detail="Current video not found in store.")

    action = "resumed" if getattr(current_video, "is_paused", False) else "paused"
    # setattr(current_video, "is_paused", not getattr(current_video, "is_paused", False)) # Simulate state change

    return ActionSuccessResponse(
        message=f"Video '{current_video.title}' {action} (simulated).",
        current_video=current_video
    )


@router.get("/current_video_link", response_model=CurrentVideoResponse, responses={404: {"model": ErrorResponse}})
async def get_current_video_details():
    """
    Возвращает информацию о текущем активном видео.
    """
    current_video = queue_manager.get_current_video_info_api()
    if current_video:
        return CurrentVideoResponse(message="Current active video details.", video_info=current_video)
    else:
        return CurrentVideoResponse(message="Video queue is empty or no video selected.", video_info=None)


@router.post("/video/{video_id_in_queue}/cancel_download", response_model=ActionSuccessResponse, responses={404: {"model": ErrorResponse}})
async def cancel_video_download(video_id_in_queue: str):
    """
    Отменяет загрузку видео (устанавливает статус).
    """
    updated_video_entry = queue_manager.cancel_download(video_id_in_queue)
    message = f"Download for '{updated_video_entry.title}' "
    if updated_video_entry.status == "metadata_fetched": # Assuming this is the status after cancellation
        message += "has been marked as cancelled."
    elif updated_video_entry.status == "downloaded":
         message = f"Video '{updated_video_entry.title}' is already downloaded. Cannot cancel."
    else:
        message = f"Video '{updated_video_entry.title}' is not currently downloading (status: {updated_video_entry.status}). No action taken."

    return ActionSuccessResponse(
        message=message,
        current_video=updated_video_entry
    )


@router.post("/video/{video_id_in_queue}/download", response_model=ActionSuccessResponse, status_code=202, responses={404: {"model": ErrorResponse}})
async def download_single_video(video_id_in_queue: str, background_tasks: BackgroundTasks):
    """
    Инициирует скачивание указанного видео из очереди.
    """
    updated_video_entry = queue_manager.initiate_download(video_id_in_queue, background_tasks)
    message = f"Download initiated for video '{updated_video_entry.title}'."
    if updated_video_entry.status == "downloaded":
        message = f"Video '{updated_video_entry.title}' is already downloaded."
    elif updated_video_entry.status == "downloading":
        message = f"Video '{updated_video_entry.title}' is already downloading."

    return ActionSuccessResponse(
        message=message,
        current_video=updated_video_entry
    )
