from fastapi import APIRouter, HTTPException, Body, BackgroundTasks
from typing import List, Optional, Dict
import uuid # Для генерации уникальных ID для видео в очереди

# Импортируем модели и функции ytdlp
from app.models.video import (
    VideoCreate, VideoInQueue, AddVideoResponse, QueueResponse,
    CurrentVideoResponse, ActionSuccessResponse, ErrorResponse, VideoInQueue
)
from app.core.ytdlp import get_video_info, download_video
from app.streaming import YTDLPSeekableStream, parse_range_header
from app.config import YDL_OPTS # Import consolidated YDL_OPTS
from app.queue_manager import VideoQueueManager # Import the new manager

import os
import stat
import asyncio
from fastapi.responses import StreamingResponse
from fastapi import Request, Header, HTTPException, Depends

router = APIRouter()

# Instantiate the VideoQueueManager
# This will hold the queue state and logic.
queue_manager = VideoQueueManager()

# --- Путь к файлу-заглушке ---
PLACEHOLDER_VIDEO_PATH = "static/offline_video.mp4" # Убедитесь, что этот файл существует

# --- Вспомогательная функция для стриминга локального файла (для заглушки) ---
async def read_file_chunked(file_path: str, start: int, end: int, chunk_size: int = 64 * 1024):
    """Читает и отдает часть файла асинхронно."""
    try:
        loop = asyncio.get_event_loop()
        file_size = (await loop.run_in_executor(None, os.stat, file_path)).st_size

        # Валидация start и end относительно file_size
        if start >= file_size:
            # print(f"Placeholder stream: Start {start} is beyond file size {file_size}. Returning empty.")
            yield b"" # Ничего не отдаем, если старт за пределами файла
            return

        actual_end = min(end, file_size - 1) # Убедимся, что не читаем за пределами файла
        bytes_to_read = actual_end - start + 1

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
                    # EOF reached earlier than expected.
                    print(f"Warning: EOF reached prematurely in read_file_chunked for {file_path}. Expected {remaining_bytes} more bytes, got 0 from read.")
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


# --- Эндпоинт для стриминга ---
@router.get("/live_stream")
async def stream_live_video(request: Request, range_header: Optional[str] = Header(None, alias="Range")):
    # Get current video URL and title from the queue manager
    active_video_url, active_video_title = queue_manager.get_current_video_details_for_stream()

    loop = asyncio.get_event_loop()

    if active_video_url:
        # Стриминг активного видео из очереди
        # YDL_OPTS will be passed to YTDLPSeekableStream
        stream = None
        try:
            # print(f"Live stream: Attempting to stream from URL: {active_video_url}")
            # Use the global YDL_OPTS from app.config
            stream = YTDLPSeekableStream(url=active_video_url, ydl_opts=YDL_OPTS.copy(), loop=loop)

            total_size = stream.total_size
            status_code = 200
            content_length = total_size
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Type": stream.format.get('mime_type') or "video/mp4", # Используем mime_type из формата
                "X-Stream-Title": active_video_title.encode('utf-8', 'surrogateescape').decode('latin-1', 'replace') if active_video_title else "Unknown"

            }
            # print(f"Total size for {active_video_url}: {total_size}")

            start_byte = 0
            end_byte = (total_size - 1) if total_size is not None else None # inclusive end

            if range_header and total_size is not None: # Range работает надежно только с известным total_size
                try:
                    start_byte, end_byte = parse_range_header(range_header, total_size)
                    await stream.seek(start_byte) # Важно сделать seek перед чтением

                    content_length = (end_byte - start_byte + 1)
                    headers["Content-Length"] = str(content_length)
                    headers["Content-Range"] = f"bytes {start_byte}-{end_byte}/{total_size}"
                    status_code = 206
                    # print(f"Live stream: Range request: {start_byte}-{end_byte}/{total_size}. Content-Length: {content_length}")
                except HTTPException as e: # parse_range_header может выкинуть HTTPException (e.g. 416)
                    if stream: await stream.close()
                    raise e
                except ValueError as e: # Ошибки парсинга Range
                    if stream: await stream.close()
                    raise HTTPException(status_code=400, detail=f"Invalid Range header: {e}")

            elif total_size is not None: # No range, but size known
                 headers["Content-Length"] = str(total_size)
            # else: total_size is None and no range_header, Content-Length не устанавливаем, браузер разберется

            async def ytdlp_content_iterator():
                try:
                    while True:
                        # Рассчитываем, сколько читать, чтобы не выйти за пределы запрошенного диапазона
                        # или общего размера файла.
                        bytes_left_in_range = float('inf')
                        if end_byte is not None: # Если есть end_byte (из Range или total_size)
                            bytes_left_in_range = (end_byte - stream.tell() + 1)

                        if bytes_left_in_range <= 0: break

                        read_amount = min(64 * 1024, bytes_left_in_range)
                        if read_amount <= 0 : break # На случай если bytes_left_in_range стал 0 или <0

                        chunk = await stream.read(int(read_amount)) # stream.read ожидает int
                        if not chunk:
                            # print(f"Live stream: No more chunks from stream.read() for {active_video_url}.")
                            break
                        yield chunk
                except HTTPException as e_stream:
                    # Handle exceptions specifically from stream.read() or other operations within the iterator
                    print(f"Live stream: HTTPException during content iteration for {active_video_url}: {e_stream.detail} (Status: {e_stream.status_code})")
                    # Optionally, you could yield a special error marker if the client is designed to handle it,
                    # but for video streams, usually just stopping is best.
                    # Do not re-raise here if response has started, as it will cause the RuntimeError.
                except Exception as e_gen:
                    print(f"Live stream: Generic exception during content iteration for {active_video_url}: {e_gen}")
                    # Similar to above, do not re-raise if response has started.
                finally:
                    # print(f"Live stream: Closing stream for {active_video_url} in iterator's finally block.")
                    if stream:
                        await stream.close()

            return StreamingResponse(ytdlp_content_iterator(), status_code=status_code, headers=headers)

        except HTTPException as e: # Catches errors from YTDLPSeekableStream setup or parse_range_header
            if stream: await stream.close() # Ensure stream is closed if created before error
            print(f"HTTPException during YTDLP streaming setup for {active_video_url}: {e.detail} (Status: {e.status_code})")
            if e.status_code in [400, 404, 502, 503]: # Errors that might warrant falling back to placeholder
                print(f"Falling back to placeholder due to YTDLP stream setup error: {e.detail}")
                active_video_url = None # Force placeholder
            else:
                raise e # Re-throw other HTTPExceptions (e.g., 416 Range Not Satisfiable)
        except Exception as e_setup: # Catches other unexpected errors during setup
            if stream: await stream.close()
            print(f"Unexpected error during YTDLP streaming setup for {active_video_url}: {e_setup}")
            active_video_url = None # Force placeholder for generic errors too


    # Если active_video_url все еще None (не было активного видео или произошла ошибка выше) -> стримим заглушку
    if not active_video_url:
        # print(f"Live stream: Serving placeholder video from {PLACEHOLDER_VIDEO_PATH}")
        if not os.path.exists(PLACEHOLDER_VIDEO_PATH):
            raise HTTPException(status_code=500, detail="Placeholder video file not found on server.")

        file_size = (await loop.run_in_executor(None, os.stat, PLACEHOLDER_VIDEO_PATH)).st_size
        status_code = 200
        content_length = file_size
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": "video/mp4", # Заглушка у нас mp4
            "X-Stream-Title": "Stream Offline"
        }

        start = 0
        end = file_size - 1 # inclusive end

        if range_header:
            try:
                print(f"DEBUG: Placeholder stream. Range header: '{range_header}', file_size: {file_size}") # ОТЛАДКА
                parsed_range = parse_range_header(range_header, file_size)
                if parsed_range is None:
                    print("ERROR: parse_range_header returned None!")
                    raise HTTPException(status_code=500, detail="Internal error: Range parsing failed unexpectedly (returned None).")
                start, end = parsed_range # end is inclusive

                content_length = (end - start + 1)

                # Валидация content_length, особенно для 0-байтных файлов
                if file_size == 0:
                    if start == 0 and end == -1: # Ожидаемый результат для "bytes=0-" или "bytes=0-0" на 0-байтном файле
                        content_length = 0
                    # Любой другой start для 0-байтного файла должен был вызвать 416 ранее в parse_range_header
                    # Но если parse_range_header вернул что-то иное, а start >= file_size (т.е. start >=0)
                    elif start >= file_size: # Должно быть 416, но если мы здесь, значит что-то не так
                         print(f"Warning: start ({start}) >= file_size ({file_size}) but no 416. Setting content_length=0.")
                         content_length = 0

                if content_length < 0:
                     print(f"ERROR: Calculated negative content_length {content_length}. Range: {start}-{end}, FileSize: {file_size}. Correcting to 0.")
                     # Это аварийное исправление, нужно понять, почему parse_range_header дал такой результат
                     content_length = 0 # Не позволяем отрицательной длине контента

                headers["Content-Length"] = str(content_length)
                headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
                status_code = 206
            except HTTPException as e:
                 raise e # например, 416
            except ValueError as e: # Ошибки парсинга Range
                 raise HTTPException(status_code=400, detail=f"Invalid Range header for placeholder: {e}")
        else:
            headers["Content-Length"] = str(file_size)

        return StreamingResponse(
            read_file_chunked(PLACEHOLDER_VIDEO_PATH, start, end), # end здесь должен быть inclusive
            status_code=status_code,
            headers=headers
        )


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
