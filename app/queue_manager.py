import logging
from typing import Dict, List, Optional, Tuple
import uuid
from fastapi import HTTPException, BackgroundTasks

from app.models.video import VideoInQueue, VideoCreate
from app.core.ytdlp import (
    get_video_info,
    download_video,
)
from app.config import YDL_OPTS


logger = logging.getLogger(__name__)

class VideoQueueManager:
    def __init__(self):
        self.video_queue_store: Dict[str, VideoInQueue] = {}
        self.ordered_queue_ids: List[str] = []
        self.current_video_id_in_queue: Optional[str] = None

    async def _fetch_and_update_metadata_task(
        self, video_id_in_queue: str, original_url: str
    ):
        logger.info(
            f"Fetching metadata for {original_url} (ID: {video_id_in_queue})")
        video_data = await get_video_info(original_url)

        video_entry = self.video_queue_store.get(video_id_in_queue)
        if not video_entry:
            logger.warning(
                f"Video ID {video_id_in_queue} not found in store after metadata fetch for {original_url}.")
            return

        if video_data:
            video_entry.title = video_data.get("title", "Unknown Title")
            video_entry.duration = video_data.get("duration")
            video_entry.thumbnail = video_data.get("thumbnail")
            video_entry.uploader = video_data.get("uploader")
            video_entry.webpage_url = video_data.get(
                "webpage_url", video_entry.original_url
            )
            video_entry.status = "metadata_fetched"
            logger.info(f"Metadata updated for '{video_entry.title}' (ID: {video_id_in_queue})")
        else:
            video_entry.status = "metadata_failed"
            video_entry.error_message = "Failed to fetch video metadata."
            logger.error(f"Failed to fetch metadata for {original_url} (ID: {video_id_in_queue})")

    async def _download_video_task(self, video_id_in_queue: str):
        video_entry = self.video_queue_store.get(video_id_in_queue)
        if not video_entry:
            logger.warning(
                f"Download task: Video ID {video_id_in_queue} not found in store.")
            return

        if video_entry.status == "downloaded":
            logger.info(f"Video '{video_entry.title}' (ID: {video_id_in_queue}) already downloaded.")
            return

        if not video_entry.original_url:
            video_entry.status = "download_failed"
            video_entry.error_message = "Cannot download, original URL is missing."
            logger.error(
                f"Cannot download '{video_entry.title}' (ID: {video_id_in_queue}), original URL missing.")
            return

        logger.info(
            f"Starting download for '{video_entry.title}' (ID: {video_id_in_queue}) from {video_entry.original_url}")
        video_entry.status = "downloading"

        output_template = YDL_OPTS.get(
            "outtmpl", "downloads/%(title)s [%(id)s].%(ext)s"
        )

        downloaded_path = await download_video(
            str(video_entry.original_url), output_path=output_template
        )

        if downloaded_path:
            video_entry.downloaded_path = downloaded_path
            video_entry.status = "downloaded"
            logger.info(
                f"Successfully downloaded '{video_entry.title}' (ID: {video_id_in_queue}) to {downloaded_path}")
        else:
            video_entry.status = "download_failed"
            video_entry.error_message = "Failed to download video."
            logger.error(f"Failed to download '{video_entry.title}' (ID: {video_id_in_queue}) from {video_entry.original_url}")

    def add_video(
        self, payload: VideoCreate, background_tasks: BackgroundTasks
    ) -> Tuple[VideoInQueue, int]:
        video_id = str(uuid.uuid4())
        new_video_entry = VideoInQueue(
            id_in_queue=video_id,
            original_url=payload.url,
            title=str(payload.url).split("/")[-1] or "Loading title...",
            status="pending_metadata",
        )

        self.video_queue_store[video_id] = new_video_entry
        self.ordered_queue_ids.append(video_id)

        background_tasks.add_task(
            self._fetch_and_update_metadata_task, video_id, str(payload.url)
        )

        if self.current_video_id_in_queue is None:
            self.current_video_id_in_queue = video_id

        position = self.ordered_queue_ids.index(video_id)
        return new_video_entry, position

    def get_queue_state(self) -> Tuple[List[VideoInQueue], Optional[str], int]:
        queue_list = [
            self.video_queue_store[vid]
            for vid in self.ordered_queue_ids
            if vid in self.video_queue_store
        ]
        return queue_list, self.current_video_id_in_queue, len(queue_list)

    def play_next_video(self) -> VideoInQueue:
        if not self.ordered_queue_ids:
            raise HTTPException(status_code=404, detail="Video queue is empty")

        if self.current_video_id_in_queue is None:
            self.current_video_id_in_queue = self.ordered_queue_ids[0]
        else:
            try:
                current_index = self.ordered_queue_ids.index(
                    self.current_video_id_in_queue
                )
                if current_index < len(self.ordered_queue_ids) - 1:
                    self.current_video_id_in_queue = self.ordered_queue_ids[
                        current_index + 1
                    ]
                else:
                    raise HTTPException(
                        status_code=404, detail="Already at the end of the queue"
                    )
            except ValueError:
                self.current_video_id_in_queue = self.ordered_queue_ids[0]

        current_video = self.video_queue_store.get(self.current_video_id_in_queue)
        if not current_video:
            raise HTTPException(
                status_code=500,
                detail="Internal server error: Current video ID not found in store.",
            )
        return current_video

    def play_previous_video(self) -> VideoInQueue:
        if not self.ordered_queue_ids:
            raise HTTPException(status_code=404, detail="Video queue is empty")

        if self.current_video_id_in_queue is None:
            raise HTTPException(
                status_code=404,
                detail="No video is currently selected to go previous from.",
            )

        try:
            current_index = self.ordered_queue_ids.index(self.current_video_id_in_queue)
            if current_index > 0:
                self.current_video_id_in_queue = self.ordered_queue_ids[
                    current_index - 1
                ]
            else:
                raise HTTPException(
                    status_code=404, detail="Already at the beginning of the queue"
                )
        except ValueError:
            self.current_video_id_in_queue = self.ordered_queue_ids[0]

        current_video = self.video_queue_store.get(self.current_video_id_in_queue)
        if not current_video:
            raise HTTPException(
                status_code=500,
                detail="Internal server error: Current video ID not found in store during previous.",
            )
        return current_video

    def get_current_video_details_for_stream(self) -> Tuple[Optional[str], str]:
        active_video_url: Optional[str] = None
        active_video_title: str = "Live Stream"

        if not self.ordered_queue_ids:
            return None, active_video_title

        if (
            self.current_video_id_in_queue is None
            or self.current_video_id_in_queue not in self.video_queue_store
        ):
            if self.ordered_queue_ids:
                self.current_video_id_in_queue = self.ordered_queue_ids[0]
            else:
                return None, active_video_title

        video_entry = self.video_queue_store.get(self.current_video_id_in_queue)

        if video_entry and video_entry.original_url:
            if video_entry.status not in ["metadata_failed", "download_failed"]:
                active_video_url = str(video_entry.original_url)
                active_video_title = video_entry.title or active_video_title
            else:
                logger.warning(
                    f"Current video '{video_entry.title}' (ID: {self.current_video_id_in_queue}) has error status '{video_entry.status}'. Will not use for live stream.")
        else:
            logger.warning(
                f"Current video entry for ID {self.current_video_id_in_queue} is invalid or has no URL. Cannot use for live stream.")

        return active_video_url, active_video_title

    def get_current_video_info_api(self) -> Optional[VideoInQueue]:
        if not self.ordered_queue_ids:
            return None

        if (
            self.current_video_id_in_queue is None
            or self.current_video_id_in_queue not in self.video_queue_store
        ):
            if self.ordered_queue_ids:
                self.current_video_id_in_queue = self.ordered_queue_ids[0]
            else:
                return None

        return self.video_queue_store.get(self.current_video_id_in_queue)

    def get_video_entry(self, video_id_in_queue: str) -> Optional[VideoInQueue]:
        return self.video_queue_store.get(video_id_in_queue)

    def initiate_download(
        self, video_id_in_queue: str, background_tasks: BackgroundTasks
    ) -> VideoInQueue:
        video_entry = self.get_video_entry(video_id_in_queue)
        if not video_entry:
            raise HTTPException(
                status_code=404,
                detail=f"Video with ID {video_id_in_queue} not found in queue.",
            )

        if video_entry.status == "downloaded":
            return video_entry

        if video_entry.status == "downloading":
            return video_entry

        if video_entry.status not in [
            "pending_metadata",
            "metadata_fetched",
            "metadata_failed",
            "download_failed",
            "pending_download",
        ]:
            logger.info(
                f"Video '{video_entry.title}' (ID: {video_id_in_queue}) in status '{video_entry.status}' - will be set to pending_download.")

        video_entry.status = "pending_download"
        background_tasks.add_task(self._download_video_task, video_id_in_queue)

        return video_entry

    def cancel_download(self, video_id_in_queue: str) -> VideoInQueue:
        video_entry = self.get_video_entry(video_id_in_queue)
        if not video_entry:
            raise HTTPException(
                status_code=404, detail=f"Video with ID {video_id_in_queue} not found."
            )

        if video_entry.status in ["downloading", "pending_download"]:
            previous_status = video_entry.status
            video_entry.status = "metadata_fetched"
            video_entry.error_message = (
                f"Download cancelled by user from status: {previous_status}"
            )
            logger.info(
                f"Download for video '{video_entry.title}' (ID: {video_id_in_queue}) marked as cancelled from status {previous_status}.")
        elif video_entry.status == "downloaded":
            logger.info(f"Video '{video_entry.title}' (ID: {video_id_in_queue}) is already downloaded, cancel request ignored.")
            pass
        else:
            pass
        return video_entry
