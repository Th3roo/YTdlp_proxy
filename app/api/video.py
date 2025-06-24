from fastapi import APIRouter, HTTPException, Body, BackgroundTasks
from typing import List, Optional, Dict
import uuid # Для генерации уникальных ID для видео в очереди

# Импортируем модели и функции ytdlp
from app.models.video import (
    VideoCreate, VideoInQueue, AddVideoResponse, QueueResponse,
    CurrentVideoResponse, ActionSuccessResponse, ErrorResponse, VideoInQueue
)
from app.core.ytdlp import get_video_info, download_video
from app.streaming import YTDLPSeekableStream, parse_range_header # Новый импорт

import os
import stat
import asyncio
from fastapi.responses import StreamingResponse
from fastapi import Request, Header, HTTPException, Depends

router = APIRouter()

# --- Управление очередью (глобальные переменные, как и были) ---
# Хранилище очереди (пока в памяти)
# Ключ - id_in_queue (uuid), значение - объект VideoInQueue
video_queue_store: Dict[str, VideoInQueue] = {}
# Список для сохранения порядка
ordered_queue_ids: List[str] = []
current_video_id_in_queue: Optional[str] = None

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

        async with await loop.run_in_executor(None, open, file_path, "rb") as f:
            await loop.run_in_executor(None, f.seek, start)
            remaining_bytes = bytes_to_read
            while remaining_bytes > 0:
                read_amount = min(chunk_size, remaining_bytes)
                chunk = await loop.run_in_executor(None, f.read, read_amount)
                if not chunk:
                    break # Конец файла раньше, чем ожидалось
                yield chunk
                remaining_bytes -= len(chunk)
    except FileNotFoundError:
        print(f"Error: Placeholder file not found at {file_path}")
        # Можно yield какой-то стандартный "error" chunk или просто ничего
        yield b""
    except Exception as e:
        print(f"Error streaming placeholder file {file_path}: {e}")
        yield b""

# Убираем дублирование глобальных переменных, они уже объявлены выше при импорте VideoInQueue
# video_queue_store: Dict[str, VideoInQueue] = {}
# ordered_queue_ids: List[str] = []
# current_video_id_in_queue: Optional[str] = None


# --- Эндпоинт для стриминга ---
@router.get("/live_stream")
async def stream_live_video(request: Request, range_header: Optional[str] = Header(None, alias="Range")):
    active_video_url = None
    active_video_title = "Live Stream" # Default title

    if current_video_id_in_queue and current_video_id_in_queue in video_queue_store:
        video_entry = video_queue_store[current_video_id_in_queue]
        if video_entry and video_entry.original_url: # Убедимся, что URL есть
            # Проверим статус, возможно, не стоит стримить, если была ошибка метаданных
            if video_entry.status not in ["metadata_failed", "download_failed"]: # TODO: решить, какие статусы блокируют стриминг
                active_video_url = str(video_entry.original_url)
                active_video_title = video_entry.title or active_video_title
            else:
                print(f"Live stream: Current video '{video_entry.title}' has error status '{video_entry.status}'. Using placeholder.")
        else:
            print(f"Live stream: Current video entry for ID {current_video_id_in_queue} is invalid or has no URL. Using placeholder.")
    else:
        print("Live stream: No active video in queue or ID is invalid. Using placeholder.")

    loop = asyncio.get_event_loop()

    if active_video_url:
        # Стриминг активного видео из очереди
        # TODO: Consider ydl_opts, e.g., cookies if needed, passed from a config or global settings
        # 'cookiesfrombrowser': ('firefox',) # Пример из вашего кода, нужно сделать настраиваемым
        default_ydl_opts = {
            "quiet": True, "noprogress": True,
            # "format": "best[height<=?720][ext=mp4]/best[ext=mp4]/best" # Пример ограничения качества
        }
        stream = None
        try:
            # print(f"Live stream: Attempting to stream from URL: {active_video_url}")
            stream = YTDLPSeekableStream(url=active_video_url, ydl_opts=default_ydl_opts, loop=loop)

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
                            break
                        yield chunk
                finally:
                    if stream:
                        await stream.close()

            return StreamingResponse(ytdlp_content_iterator(), status_code=status_code, headers=headers)

        except HTTPException as e: # Перехватываем ошибки от YTDLPSeekableStream или parse_range_header
            if stream: await stream.close()
            print(f"HTTPException during YTDLP streaming setup for {active_video_url}: {e.detail}")
            # Если стриминг основного видео не удался, можно попробовать отдать заглушку
            # Но это усложнит логику, пока просто пробрасываем ошибку или падаем на заглушку ниже, если active_video_url станет None
            if e.status_code == 404 or e.status_code == 502 or e.status_code == 400: # Ошибки, после которых стоит попробовать заглушку
                print(f"Falling back to placeholder due to YTDLP stream error: {e.detail}")
                active_video_url = None # Принудительно переключаемся на заглушку
            else:
                raise e # Пробрасываем другие ошибки (например, 416)
        except Exception as e:
            if stream: await stream.close()
            print(f"Unexpected error during YTDLP streaming setup for {active_video_url}: {e}")
            # Также падаем на заглушку
            active_video_url = None


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


async def _fetch_and_update_metadata(video_id_in_queue: str, original_url: str):
    """
    Фоновая задача для получения метаданных и обновления информации о видео в очереди.
    """
    print(f"Fetching metadata for {original_url} (ID: {video_id_in_queue})")
    video_data = await get_video_info(original_url)

    video_entry = video_queue_store.get(video_id_in_queue)
    if not video_entry:
        print(f"Video ID {video_id_in_queue} not found in store after metadata fetch.")
        return

    if video_data:
        video_entry.title = video_data.get("title", "Unknown Title")
        video_entry.duration = video_data.get("duration")
        video_entry.thumbnail = video_data.get("thumbnail")
        video_entry.uploader = video_data.get("uploader")
        video_entry.webpage_url = video_data.get("webpage_url", video_entry.original_url)
        video_entry.status = "metadata_fetched" # Или "ready_to_play" если не планируем скачивание перед этим
        print(f"Metadata updated for {video_entry.title}")
    else:
        video_entry.status = "metadata_failed"
        video_entry.error_message = "Failed to fetch video metadata."
        print(f"Failed to fetch metadata for {original_url}")

    # Здесь можно добавить логику для автоматического скачивания, если нужно
    # Например: if video_entry.status == "metadata_fetched": await _initiate_download_task(video_id_in_queue, background_tasks)


async def _initiate_download_task(video_id_in_queue: str, background_tasks: BackgroundTasks):
    """Helper to add download task to background."""
    video_entry = video_queue_store.get(video_id_in_queue)
    if video_entry and video_entry.status not in ["downloading", "downloaded", "download_failed"]:
        background_tasks.add_task(_download_video_and_update_status, video_id_in_queue)

async def _download_video_and_update_status(video_id_in_queue: str):
    """
    Фоновая задача для скачивания видео и обновления его статуса.
    """
    video_entry = video_queue_store.get(video_id_in_queue)
    if not video_entry:
        print(f"Download task: Video ID {video_id_in_queue} not found in store.")
        return

    if video_entry.status == "downloaded":
        print(f"Download task: Video {video_entry.title} already downloaded.")
        return

    print(f"Starting download for {video_entry.title} (ID: {video_id_in_queue})")
    video_entry.status = "downloading"

    # Определяем путь для скачивания. Можно сделать его более настраиваемым.
    # Важно, чтобы title и id были "безопасными" для имени файла.
    # yt-dlp сам обрабатывает санацию, если плейсхолдеры используются правильно.
    # Пример: "downloads/Название видео [VIDEO_ID].mp4"
    # Убедимся, что video_entry.id (если это ID от YouTube) или video_entry.title не содержат плохих символов
    # Но yt-dlp должен это делать сам.
    # Мы используем id_in_queue (UUID) для уникальности в нашей системе, а не ID от видеосервиса.
    # Для имени файла лучше использовать title и ID от видеосервиса, если они есть.
    # Пусть yt-dlp использует свой стандартный шаблон, если title доступен.
    # Либо можно сформировать имя файла на основе title и video_id_in_queue для гарантии уникальности.

    # Простой шаблон: downloads/ID_В_ОЧЕРЕДИ_-_Название.расширение
    # Это гарантирует уникальность по нашему ID и читаемость.
    # yt-dlp заменит %(title)s и %(ext)s. Мы добавим наш ID вручную в путь.
    # filename_template = f"downloads/{video_id_in_queue} - %(title)s.%(ext)s"
    # Или, если хотим, чтобы yt-dlp использовал свой ID:
    filename_template = f"downloads/%(title)s [%(id)s].%(ext)s"


    downloaded_path = await download_video(str(video_entry.original_url), output_path=filename_template)

    if downloaded_path:
        video_entry.downloaded_path = downloaded_path
        video_entry.status = "downloaded"
        print(f"Successfully downloaded {video_entry.title} to {downloaded_path}")
    else:
        video_entry.status = "download_failed"
        video_entry.error_message = "Failed to download video."
        print(f"Failed to download {video_entry.title}")


@router.post("/video/add", response_model=AddVideoResponse, status_code=202) # 202 Accepted для фоновой задачи
async def add_video_to_queue(payload: VideoCreate = Body(...), background_tasks: BackgroundTasks = BackgroundTasks()):
    """
    Добавляет видео в очередь по URL и запускает фоновую задачу для получения метаданных.
    """
    video_id = str(uuid.uuid4())

    new_video_entry = VideoInQueue(
        id_in_queue=video_id,
        original_url=payload.url,
        title=str(payload.url).split("/")[-1] or "Loading title...", # Временный title
        status="pending_metadata"
    )

    video_queue_store[video_id] = new_video_entry
    ordered_queue_ids.append(video_id)

    # Запускаем получение метаданных в фоне
    background_tasks.add_task(_fetch_and_update_metadata, video_id, str(payload.url))

    return AddVideoResponse(
        message="Video added to queue, fetching metadata.",
        video_info=new_video_entry,
        queue_position=ordered_queue_ids.index(video_id)
    )

@router.get("/queue", response_model=QueueResponse)
async def get_queue_state():
    """
    Возвращает текущее состояние очереди видео.
    """
    queue_list = [video_queue_store[vid] for vid in ordered_queue_ids if vid in video_queue_store]
    return QueueResponse(
        queue=queue_list,
        current_video_id_in_queue=current_video_id_in_queue,
        total_items=len(queue_list)
    )

@router.post("/video/play_next", response_model=ActionSuccessResponse, responses={404: {"model": ErrorResponse}})
async def play_next_video():
    """
    Переключает на следующее видео в очереди.
    """
    global current_video_id_in_queue
    if not ordered_queue_ids:
        raise HTTPException(status_code=404, detail="Video queue is empty")

    if current_video_id_in_queue is None: # Если ничего не играло, начинаем с первого
        current_video_id_in_queue = ordered_queue_ids[0]
    else:
        try:
            current_index = ordered_queue_ids.index(current_video_id_in_queue)
            if current_index < len(ordered_queue_ids) - 1:
                current_video_id_in_queue = ordered_queue_ids[current_index + 1]
            else:
                # Достигнут конец очереди, можно остановиться или начать сначала
                # Пока просто сообщаем, что это конец.
                # current_video_id_in_queue = ordered_queue_ids[0] # для зацикливания
                # return ActionSuccessResponse(message="Reached end of queue, restarting.", current_video=video_queue_store.get(current_video_id_in_queue))
                raise HTTPException(status_code=404, detail="Already at the end of the queue")
        except ValueError: # Текущий ID не найден в упорядоченном списке (маловероятно)
            current_video_id_in_queue = ordered_queue_ids[0]


    current_video = video_queue_store.get(current_video_id_in_queue)
    return ActionSuccessResponse(message="Playing next video", current_video=current_video)


@router.post("/video/play_previous", response_model=ActionSuccessResponse, responses={404: {"model": ErrorResponse}})
async def play_previous_video():
    """
    Переключает на предыдущее видео в очереди.
    """
    global current_video_id_in_queue
    if not ordered_queue_ids:
        raise HTTPException(status_code=404, detail="Video queue is empty")

    if current_video_id_in_queue is None: # Если ничего не играло, нельзя выбрать предыдущее
        raise HTTPException(status_code=404, detail="No video is currently selected to go previous from.")
    else:
        try:
            current_index = ordered_queue_ids.index(current_video_id_in_queue)
            if current_index > 0:
                current_video_id_in_queue = ordered_queue_ids[current_index - 1]
            else:
                raise HTTPException(status_code=404, detail="Already at the beginning of the queue")
        except ValueError:
             # Текущий ID не найден, возможно очередь была изменена, начнем с первого
            current_video_id_in_queue = ordered_queue_ids[0]
            # Или можно вызвать ошибку, если такое поведение нежелательно

    current_video = video_queue_store.get(current_video_id_in_queue)
    return ActionSuccessResponse(message="Playing previous video", current_video=current_video)


@router.post("/video/pause_resume", response_model=ActionSuccessResponse, responses={404: {"model": ErrorResponse}})
async def pause_resume_video():
    """
    Ставит на паузу/возобновляет текущее видео.
    (Пока это заглушка, т.к. реальное управление плеером не реализовано на бэкенде)
    """
    if not current_video_id_in_queue or current_video_id_in_queue not in video_queue_store:
        raise HTTPException(status_code=404, detail="No video is currently active or selected")

    current_video = video_queue_store.get(current_video_id_in_queue)
    # Здесь в будущем будет логика изменения состояния (например, current_video.is_paused = not current_video.is_paused)
    # и возможно взаимодействие с внешним плеером.
    # Пока просто возвращаем сообщение.
    action = "resumed" if getattr(current_video, "is_paused", False) else "paused" # Пример
    # setattr(current_video, "is_paused", not getattr(current_video, "is_paused", False)) # Изменить состояние (если бы оно было)

    return ActionSuccessResponse(
        message=f"Video '{current_video.title}' {action} (simulated).",
        current_video=current_video
    )


@router.get("/current_video_link", response_model=CurrentVideoResponse, responses={404: {"model": ErrorResponse}})
async def get_current_video_details():
    """
    Возвращает информацию о текущем активном видео.
    Если очередь не пуста и видео не выбрано, выбирает первое видео.
    """
    global current_video_id_in_queue
    if not ordered_queue_ids: # Очередь пуста
        return CurrentVideoResponse(message="Video queue is empty", video_info=None)
        # Или можно вернуть 404: raise HTTPException(status_code=404, detail="Video queue is empty")

    if current_video_id_in_queue is None or current_video_id_in_queue not in video_queue_store:
        # Если видео не выбрано или ID невалиден, но очередь не пуста, выбираем первое
        current_video_id_in_queue = ordered_queue_ids[0]

    current_video = video_queue_store.get(current_video_id_in_queue)
    if current_video:
        return CurrentVideoResponse(message="Current active video details.", video_info=current_video)
    else:
        # Эта ветка маловероятна, если current_video_id_in_queue всегда валиден из ordered_queue_ids
        current_video_id_in_queue = None # Сброс, если ID оказался недействительным
        return CurrentVideoResponse(message="No video currently selected, or selected video not found.", video_info=None)
        # Или: raise HTTPException(status_code=404, detail="Current video not found, though ID was set.")

@router.post("/video/{video_id_in_queue}/cancel_download", response_model=ActionSuccessResponse, responses={404: {"model": ErrorResponse}})
async def cancel_video_download(video_id_in_queue: str):
    """
    Отменяет загрузку видео (устанавливает статус).
    Не прерывает уже запущенный процесс yt-dlp, но меняет статус для UI и будущих действий.
    """
    if video_id_in_queue not in video_queue_store:
        raise HTTPException(status_code=404, detail=f"Video with ID {video_id_in_queue} not found.")

    video_entry = video_queue_store[video_id_in_queue]

    if video_entry.status in ["downloading", "pending_download"]:
        # Возвращаем статус к "metadata_fetched", чтобы можно было попробовать скачать снова
        # или можно ввести новый статус "download_cancelled"
        previous_status = video_entry.status
        video_entry.status = "metadata_fetched" # или "download_cancelled"
        video_entry.error_message = f"Download cancelled by user from status: {previous_status}"
        # TODO: Если бы у нас был способ реально остановить yt-dlp, здесь была бы эта логика.
        # Например, если бы _download_video_and_update_status проверяла какой-то флаг отмены.
        # Для данного MVP, мы просто меняем статус. Частично скачанный файл может остаться.
        print(f"Download for video '{video_entry.title}' (ID: {video_id_in_queue}) marked as cancelled.")
        return ActionSuccessResponse(
            message=f"Download for '{video_entry.title}' has been marked as cancelled.",
            current_video=video_entry
        )
    elif video_entry.status == "downloaded":
        return ActionSuccessResponse(
            message=f"Video '{video_entry.title}' is already downloaded. Cannot cancel.",
            current_video=video_entry
        )
    else:
        return ActionSuccessResponse(
            message=f"Video '{video_entry.title}' is not currently downloading (status: {video_entry.status}). No action taken.",
            current_video=video_entry
        )


@router.post("/video/{video_id_in_queue}/download", response_model=ActionSuccessResponse, status_code=202, responses={404: {"model": ErrorResponse}})
async def download_single_video(video_id_in_queue: str, background_tasks: BackgroundTasks):
    """
    Инициирует скачивание указанного видео из очереди.
    """
    video_entry = video_queue_store.get(video_id_in_queue)
    if not video_entry:
        raise HTTPException(status_code=404, detail=f"Video with ID {video_id_in_queue} not found in queue.")

    if video_entry.status == "downloaded":
        return ActionSuccessResponse(
            message=f"Video '{video_entry.title}' is already downloaded.",
            current_video=video_entry # Возвращаем информацию о видео
        )

    if video_entry.status == "downloading":
        return ActionSuccessResponse(
            message=f"Video '{video_entry.title}' is already downloading.",
            current_video=video_entry
        )

    # Используем существующий хелпер для запуска задачи в фоне
    await _initiate_download_task(video_id_in_queue, background_tasks)

    video_entry.status = "pending_download" # Устанавливаем статус ожидания начала фактической загрузки
                                            # _download_video_and_update_status изменит на "downloading"
    return ActionSuccessResponse(
        message=f"Download initiated for video '{video_entry.title}'.",
        current_video=video_entry # Возвращаем обновленную информацию о видео
    )
