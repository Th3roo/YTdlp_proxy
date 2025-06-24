from typing import Dict, List, Optional, Tuple
import uuid
from fastapi import HTTPException, BackgroundTasks

from app.models.video import VideoInQueue, VideoCreate
from app.core.ytdlp import get_video_info, download_video # Import actual ytdlp functions


class VideoQueueManager:
    def __init__(self):
        # Хранилище очереди (ключ - id_in_queue (uuid), значение - объект VideoInQueue)
        self.video_queue_store: Dict[str, VideoInQueue] = {}
        # Список для сохранения порядка
        self.ordered_queue_ids: List[str] = []
        self.current_video_id_in_queue: Optional[str] = None

    # --- Background Task Methods ---

    async def _fetch_and_update_metadata_task(self, video_id_in_queue: str, original_url: str):
        """
        Background task to fetch metadata and update video entry.
        """
        print(f"QueueManager: Fetching metadata for {original_url} (ID: {video_id_in_queue})")
        video_data = await get_video_info(original_url) # Uses imported get_video_info

        video_entry = self.video_queue_store.get(video_id_in_queue)
        if not video_entry:
            print(f"QueueManager: Video ID {video_id_in_queue} not found in store after metadata fetch.")
            return

        if video_data:
            video_entry.title = video_data.get("title", "Unknown Title")
            video_entry.duration = video_data.get("duration")
            video_entry.thumbnail = video_data.get("thumbnail")
            video_entry.uploader = video_data.get("uploader")
            video_entry.webpage_url = video_data.get("webpage_url", video_entry.original_url)
            video_entry.status = "metadata_fetched"
            print(f"QueueManager: Metadata updated for {video_entry.title}")
        else:
            video_entry.status = "metadata_failed"
            video_entry.error_message = "Failed to fetch video metadata."
            print(f"QueueManager: Failed to fetch metadata for {original_url}")

    async def _download_video_task(self, video_id_in_queue: str):
        """
        Background task to download a video and update its status.
        """
        video_entry = self.video_queue_store.get(video_id_in_queue)
        if not video_entry:
            print(f"QueueManager: Download task - Video ID {video_id_in_queue} not found.")
            return

        if video_entry.status == "downloaded":
            print(f"QueueManager: Video {video_entry.title} already downloaded.")
            return

        if not video_entry.original_url: # Should not happen if metadata was fetched
            video_entry.status = "download_failed"
            video_entry.error_message = "Cannot download, original URL is missing."
            print(f"QueueManager: Cannot download {video_entry.title}, original URL missing.")
            return

        print(f"QueueManager: Starting download for {video_entry.title} (ID: {video_id_in_queue})")
        video_entry.status = "downloading"

        filename_template = f"downloads/%(title)s [%(id)s].%(ext)s"
        downloaded_path = await download_video(str(video_entry.original_url), output_path=filename_template)

        if downloaded_path:
            video_entry.downloaded_path = downloaded_path
            video_entry.status = "downloaded"
            print(f"QueueManager: Successfully downloaded {video_entry.title} to {downloaded_path}")
        else:
            video_entry.status = "download_failed"
            video_entry.error_message = "Failed to download video."
            print(f"QueueManager: Failed to download {video_entry.title}")

    # --- Public Methods for Queue Operations ---

    def add_video(self, payload: VideoCreate, background_tasks: BackgroundTasks) -> Tuple[VideoInQueue, int]:
        """
        Adds a video to the queue and schedules metadata fetching.
        Returns the new video entry and its queue position.
        """
        video_id = str(uuid.uuid4())
        new_video_entry = VideoInQueue(
            id_in_queue=video_id,
            original_url=payload.url,
            title=str(payload.url).split("/")[-1] or "Loading title...", # Temporary title
            status="pending_metadata"
        )

        self.video_queue_store[video_id] = new_video_entry
        self.ordered_queue_ids.append(video_id)

        # Schedule metadata fetching
        background_tasks.add_task(self._fetch_and_update_metadata_task, video_id, str(payload.url))

        position = self.ordered_queue_ids.index(video_id)
        return new_video_entry, position

    def get_queue_state(self) -> Tuple[List[VideoInQueue], Optional[str], int]:
        """Returns the current state of the queue."""
        queue_list = [self.video_queue_store[vid] for vid in self.ordered_queue_ids if vid in self.video_queue_store]
        return queue_list, self.current_video_id_in_queue, len(queue_list)

    def play_next_video(self) -> VideoInQueue:
        """Switches to the next video in the queue."""
        if not self.ordered_queue_ids:
            raise HTTPException(status_code=404, detail="Video queue is empty")

        if self.current_video_id_in_queue is None:
            self.current_video_id_in_queue = self.ordered_queue_ids[0]
        else:
            try:
                current_index = self.ordered_queue_ids.index(self.current_video_id_in_queue)
                if current_index < len(self.ordered_queue_ids) - 1:
                    self.current_video_id_in_queue = self.ordered_queue_ids[current_index + 1]
                else:
                    raise HTTPException(status_code=404, detail="Already at the end of the queue")
            except ValueError: # Current ID not found
                self.current_video_id_in_queue = self.ordered_queue_ids[0]

        current_video = self.video_queue_store.get(self.current_video_id_in_queue)
        if not current_video:
            # This case should ideally not happen if logic is correct
            raise HTTPException(status_code=500, detail="Internal server error: Current video ID not found in store.")
        return current_video

    def play_previous_video(self) -> VideoInQueue:
        """Switches to the previous video in the queue."""
        if not self.ordered_queue_ids:
            raise HTTPException(status_code=404, detail="Video queue is empty")

        if self.current_video_id_in_queue is None:
            raise HTTPException(status_code=404, detail="No video is currently selected to go previous from.")

        try:
            current_index = self.ordered_queue_ids.index(self.current_video_id_in_queue)
            if current_index > 0:
                self.current_video_id_in_queue = self.ordered_queue_ids[current_index - 1]
            else:
                raise HTTPException(status_code=404, detail="Already at the beginning of the queue")
        except ValueError: # Current ID not found
            self.current_video_id_in_queue = self.ordered_queue_ids[0]

        current_video = self.video_queue_store.get(self.current_video_id_in_queue)
        if not current_video:
             raise HTTPException(status_code=500, detail="Internal server error: Current video ID not found in store during previous.")
        return current_video

    def get_current_video_details_for_stream(self) -> Tuple[Optional[str], str]:
        """
        Gets the URL and title of the current video for streaming.
        If no video is active, selects the first one.
        Returns (None, "Default Title") if queue is empty or video has issues.
        """
        active_video_url: Optional[str] = None
        active_video_title: str = "Live Stream" # Default title

        if not self.ordered_queue_ids:
            return None, active_video_title # No videos in queue

        if self.current_video_id_in_queue is None or self.current_video_id_in_queue not in self.video_queue_store:
            # If video not selected or ID invalid, but queue not empty, select first
            if self.ordered_queue_ids:
                 self.current_video_id_in_queue = self.ordered_queue_ids[0]
            else: # Should be caught by the first check, but as a safeguard
                return None, active_video_title


        video_entry = self.video_queue_store.get(self.current_video_id_in_queue)

        if video_entry and video_entry.original_url:
            if video_entry.status not in ["metadata_failed", "download_failed"]:
                active_video_url = str(video_entry.original_url)
                active_video_title = video_entry.title or active_video_title
            else:
                print(f"QueueManager: Current video '{video_entry.title}' has error status '{video_entry.status}'.")
                # active_video_url remains None, placeholder will be used
        else:
            print(f"QueueManager: Current video entry for ID {self.current_video_id_in_queue} is invalid or has no URL.")
            # active_video_url remains None, placeholder will be used

        return active_video_url, active_video_title


    def get_current_video_info_api(self) -> Optional[VideoInQueue]:
        """
        Returns full info for the current video for API response.
        Selects first video if none is current and queue is not empty.
        """
        if not self.ordered_queue_ids:
            return None # Queue is empty

        if self.current_video_id_in_queue is None or self.current_video_id_in_queue not in self.video_queue_store:
            if self.ordered_queue_ids:
                self.current_video_id_in_queue = self.ordered_queue_ids[0]
            else: # Should not happen if first check passed
                return None

        return self.video_queue_store.get(self.current_video_id_in_queue)

    def get_video_entry(self, video_id_in_queue: str) -> Optional[VideoInQueue]:
        """Gets a specific video entry by its queue ID."""
        return self.video_queue_store.get(video_id_in_queue)

    def initiate_download(self, video_id_in_queue: str, background_tasks: BackgroundTasks) -> VideoInQueue:
        """
        Initiates download for a specific video.
        Returns the video entry.
        Raises HTTPException if video not found or in invalid state.
        """
        video_entry = self.get_video_entry(video_id_in_queue)
        if not video_entry:
            raise HTTPException(status_code=404, detail=f"Video with ID {video_id_in_queue} not found in queue.")

        if video_entry.status == "downloaded":
            # Optionally, could just return info without error if re-requesting download for already downloaded.
            # For now, let's be explicit.
            # raise HTTPException(status_code=400, detail=f"Video '{video_entry.title}' is already downloaded.")
            return video_entry # Or return a message indicating it's already downloaded

        if video_entry.status == "downloading":
            # raise HTTPException(status_code=400, detail=f"Video '{video_entry.title}' is already downloading.")
            return video_entry # Or return a message

        # Valid states to start download: pending_metadata (if URL known), metadata_fetched, metadata_failed (retry), download_failed (retry)
        if video_entry.status not in ["pending_metadata", "metadata_fetched", "metadata_failed", "download_failed", "pending_download"]:
             print(f"QueueManager: Video '{video_entry.title}' in status '{video_entry.status}' cannot start download directly without status change.")
             # Allow download initiation even from failed states, it will try again.

        video_entry.status = "pending_download" # Set status before adding task
        background_tasks.add_task(self._download_video_task, video_id_in_queue)

        return video_entry

    def cancel_download(self, video_id_in_queue: str) -> VideoInQueue:
        """
        Marks a video download as cancelled.
        Does not actually stop an in-progress yt-dlp process.
        Returns the video entry.
        Raises HTTPException if video not found.
        """
        video_entry = self.get_video_entry(video_id_in_queue)
        if not video_entry:
            raise HTTPException(status_code=404, detail=f"Video with ID {video_id_in_queue} not found.")

        if video_entry.status in ["downloading", "pending_download"]:
            previous_status = video_entry.status
            video_entry.status = "metadata_fetched" # Revert to a state where download can be re-initiated
            video_entry.error_message = f"Download cancelled by user from status: {previous_status}"
            print(f"QueueManager: Download for video '{video_entry.title}' (ID: {video_id_in_queue}) marked as cancelled.")
        elif video_entry.status == "downloaded":
            # No action, or could raise error that it's already downloaded
            pass
        else:
            # No action if not in a downloadable state
            pass
        return video_entry

# Global instance (or use FastAPI dependency injection)
# This line is commented out as the instance is created in app/api/video.py
# queue_manager = VideoQueueManager()
