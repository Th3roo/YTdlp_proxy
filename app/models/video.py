from pydantic import BaseModel, HttpUrl
from typing import Optional, List, Any

class VideoBase(BaseModel):
    original_url: HttpUrl
    title: Optional[str] = None
    duration: Optional[int] = None # в секундах
    thumbnail: Optional[HttpUrl] = None
    uploader: Optional[str] = None

class VideoCreate(BaseModel): # Модель для запроса на добавление видео
    url: HttpUrl

class VideoInQueue(VideoBase):
    id_in_queue: str # Уникальный идентификатор в очереди, можно использовать UUID
    status: str # например, "pending_metadata", "pending_download", "downloaded", "failed"
    webpage_url: Optional[HttpUrl] = None # URL страницы видео (может отличаться от original_url для плейлистов)
    downloaded_path: Optional[str] = None # Путь к скачанному файлу
    error_message: Optional[str] = None # Сообщение об ошибке, если что-то пошло не так

class VideoInfo(VideoBase): # Модель для ответа с информацией о видео из yt-dlp
    id: Optional[str] = None # ID видео от сервиса (youtube ID и т.п.)
    webpage_url: HttpUrl
    formats: Optional[List[Any]] = None # Информация о доступных форматах

# Модель для ответа API при добавлении видео
class AddVideoResponse(BaseModel):
    message: str
    video_info: VideoInQueue
    queue_position: int

# Модель для ответа API со списком видео в очереди
class QueueResponse(BaseModel):
    queue: List[VideoInQueue]
    current_video_id_in_queue: Optional[str] = None
    total_items: int

# Модель для ответа API о текущем видео
class CurrentVideoResponse(BaseModel):
    message: str
    video_info: Optional[VideoInQueue] = None

# Модель для общего ответа об успехе операции
class ActionSuccessResponse(BaseModel):
    message: str
    current_video: Optional[VideoInQueue] = None
    next_video: Optional[VideoInQueue] = None # Для play_next/previous
    previous_video: Optional[VideoInQueue] = None # Для play_next/previous

class ErrorResponse(BaseModel):
    detail: str
