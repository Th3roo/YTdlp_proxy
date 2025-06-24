from fastapi import APIRouter, HTTPException, Body, BackgroundTasks
from typing import List, Optional, Dict
import uuid # Для генерации уникальных ID для видео в очереди

# Импортируем модели и функции ytdlp
from app.models.video import (
    VideoCreate, VideoInQueue, AddVideoResponse, QueueResponse,
    CurrentVideoResponse, ActionSuccessResponse, ErrorResponse
)
from app.core.ytdlp import get_video_info, download_video

router = APIRouter()

# Хранилище очереди (пока в памяти)
# Ключ - id_in_queue (uuid), значение - объект VideoInQueue
video_queue_store: Dict[str, VideoInQueue] = {}
# Список для сохранения порядка
ordered_queue_ids: List[str] = []
current_video_id_in_queue: Optional[str] = None


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
