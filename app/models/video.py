from pydantic import (
    BaseModel,
    HttpUrl,
)
from typing import Optional, List, Any


class VideoBase(BaseModel):
    original_url: str
    title: Optional[str] = None
    duration: Optional[int] = None  # in seconds
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None


class VideoCreate(BaseModel):
    url: str


class VideoInQueue(VideoBase):
    id_in_queue: str  # Unique identifier in the queue, UUID can be used
    status: str  # e.g., "pending_metadata", "pending_download", "downloaded", "failed"
    webpage_url: Optional[
        str
    ] = None  # URL of the video page (may differ from original_url for playlists)
    downloaded_path: Optional[str] = None  # Path to the downloaded file
    error_message: Optional[str] = None  # Error message if something went wrong


class VideoInfo(VideoBase):
    id: Optional[str] = None  # Video ID from the service (e.g., YouTube ID)
    webpage_url: Optional[str] = None
    formats: Optional[List[Any]] = None  # Information about available formats


class AddVideoResponse(BaseModel):
    message: str
    video_info: VideoInQueue
    queue_position: int


class QueueResponse(BaseModel):
    queue: List[VideoInQueue]
    current_video_id_in_queue: Optional[str] = None
    total_items: int


class CurrentVideoResponse(BaseModel):
    message: str
    video_info: Optional[VideoInQueue] = None


class ActionSuccessResponse(BaseModel):
    message: str
    current_video: Optional[VideoInQueue] = None
    next_video: Optional[VideoInQueue] = None
    previous_video: Optional[VideoInQueue] = None


class ErrorResponse(BaseModel):
    detail: str
