from pydantic import BaseModel, HttpUrl # HttpUrl might still be useful for input validation if desired elsewhere
from typing import Optional, List, Any

class VideoBase(BaseModel):
    original_url: str # Changed from HttpUrl
    title: Optional[str] = None
    duration: Optional[int] = None # в секундах
    thumbnail: Optional[str] = None # Changed from HttpUrl
    uploader: Optional[str] = None

class VideoCreate(BaseModel): # Модель для запроса на добавление видео
    url: str # Changed from HttpUrl - assumes client sends string. If client sends validated HttpUrl, this could stay. Given warnings, string is safer.

class VideoInQueue(VideoBase):
    id_in_queue: str # Уникальный идентификатор в очереди, можно использовать UUID
    status: str # например, "pending_metadata", "pending_download", "downloaded", "failed"
    webpage_url: Optional[str] = None # Changed from HttpUrl - URL страницы видео (может отличаться от original_url для плейлистов)
    downloaded_path: Optional[str] = None # Путь к скачанному файлу
    error_message: Optional[str] = None # Сообщение об ошибке, если что-то пошло не так

class VideoInfo(VideoBase): # Модель для ответа с информацией о видео из yt-dlp
    # This model seems to be more of an internal representation or for a direct yt-dlp info dump.
    # Its fields inherit from VideoBase which are now string.
    id: Optional[str] = None # ID видео от сервиса (youtube ID и т.п.)
    # webpage_url is inherited from VideoBase (original_url there) or should be explicitly str if it's different
    # If it represents the 'webpage_url' key directly from yt-dlp, and VideoBase.original_url is for the input URL,
    # then it should be:
    webpage_url: Optional[str] = None # Explicitly str, to match VideoInQueue.webpage_url
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
