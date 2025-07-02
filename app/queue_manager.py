import logging
import os
import re
import uuid
from typing import Dict, List, Optional, Tuple

from fastapi import BackgroundTasks, HTTPException

from app.config import YDL_OPTS
from app.core.ytdlp import download_video, get_video_info
from app.models.video import VideoCreate, VideoInQueue

logger = logging.getLogger(__name__)

# Определяем константу для папки с загрузками
DOWNLOADS_DIR = "downloads"


class VideoQueueManager:
    def __init__(self):
        self.video_queue_store: Dict[str, VideoInQueue] = {}
        self.ordered_queue_ids: List[str] = []
        self.current_video_id_in_queue: Optional[str] = None
        # Убедимся, что папка для загрузок существует при старте
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    def _find_existing_video_file(self, video_id: str) -> Optional[str]:
        """
        Ищет в папке DOWNLOADS_DIR файл, содержащий ID видео.
        Ищет по шаблону *[video_id].*
        """
        if not video_id:
            return None

        # Ищем файл, который содержит `[video_id]` в названии.
        # Это стандартный формат для yt-dlp, если используется шаблон '%(id)s'.
        pattern = re.compile(f".*\\[{re.escape(video_id)}\\].*")

        try:
            for filename in os.listdir(DOWNLOADS_DIR):
                if pattern.match(filename):
                    full_path = os.path.join(DOWNLOADS_DIR, filename)
                    logger.info(
                        f"Найден существующий файл для видео ID {video_id}: {full_path}"
                    )
                    return full_path
        except FileNotFoundError:
            logger.warning(
                f"Папка для загрузок '{DOWNLOADS_DIR}' не найдена во время поиска."
            )
            return None

        logger.info(f"Локальный файл для видео ID {video_id} не найден.")
        return None

    async def add_video(self, payload: VideoCreate) -> Tuple[VideoInQueue, int]:
        """
        Асинхронно добавляет видео, сразу получает метаданные и проверяет наличие файла.
        """
        logger.info(f"Начинаем обработку добавления видео по URL: {payload.url}")
        video_info = await get_video_info(payload.url)
        video_id_in_queue = str(uuid.uuid4())

        if not video_info or not video_info.get("id"):
            logger.error(f"Не удалось получить метаданные или ID для URL: {payload.url}")
            new_video_entry = VideoInQueue(
                id_in_queue=video_id_in_queue,
                original_url=payload.url,
                title=f"Ошибка: Не удалось получить данные для URL",
                status="metadata_failed",
                error_message="Failed to fetch video metadata or video ID.",
            )
        else:
            youtube_id = video_info.get("id")
            existing_file_path = self._find_existing_video_file(youtube_id)

            base_info = {
                "id_in_queue": video_id_in_queue,
                "original_url": video_info.get("original_url", payload.url),
                "webpage_url": video_info.get("webpage_url"),
                "title": video_info.get("title", "Unknown Title"),
                "duration": video_info.get("duration"),
                "thumbnail": video_info.get("thumbnail"),
                "uploader": video_info.get("uploader"),
            }

            if existing_file_path:
                new_video_entry = VideoInQueue(
                    **base_info,
                    status="downloaded",
                    downloaded_path=existing_file_path,
                )
                logger.info(f"Видео '{new_video_entry.title}' уже существует локально.")
            else:
                new_video_entry = VideoInQueue(**base_info, status="metadata_fetched")
                logger.info(
                    f"Метаданные для '{new_video_entry.title}' получены, файл не найден."
                )

        self.video_queue_store[video_id_in_queue] = new_video_entry
        self.ordered_queue_ids.append(video_id_in_queue)

        if self.current_video_id_in_queue is None and self.ordered_queue_ids:
            self.current_video_id_in_queue = self.ordered_queue_ids[0]

        position = self.ordered_queue_ids.index(video_id_in_queue)
        return new_video_entry, position

    async def _download_video_task(self, video_id_in_queue: str):
        video_entry = self.get_video_entry(video_id_in_queue)
        if not video_entry or not video_entry.original_url:
            logger.error(f"Невозможно скачать видео {video_id_in_queue}: нет данных или URL.")
            if video_entry:
                video_entry.status = "download_failed"
                video_entry.error_message = "Original URL is missing."
            return

        if video_entry.status == "downloaded":
            logger.info(f"Видео '{video_entry.title}' уже скачано.")
            return

        logger.info(f"Начало загрузки '{video_entry.title}'")
        video_entry.status = "downloading"

        output_template = YDL_OPTS.get(
            "outtmpl", f"{DOWNLOADS_DIR}/%(title)s [%(id)s].%(ext)s"
        )

        downloaded_path = await download_video(
            video_entry.original_url, output_path=output_template
        )

        # Перепроверяем наличие entry после асинхронной операции
        video_entry = self.get_video_entry(video_id_in_queue)
        if not video_entry:
            logger.warning(f"Видео {video_id_in_queue} было удалено во время скачивания.")
            return
            
        # Проверяем, не была ли отменена загрузка пока она шла
        if video_entry.status != 'downloading':
            logger.warning(f"Статус загрузки для '{video_entry.title}' изменился на '{video_entry.status}' во время скачивания. Прерываем обновление статуса.")
            return

        if downloaded_path and os.path.exists(downloaded_path):
            video_entry.downloaded_path = downloaded_path
            video_entry.status = "downloaded"
            logger.info(f"Успешно скачано: '{video_entry.title}'")
        else:
            video_entry.status = "download_failed"
            video_entry.error_message = "yt-dlp не вернул путь или файл не найден после скачивания."
            logger.error(f"Ошибка скачивания '{video_entry.title}'")

    def get_queue_state(self) -> Tuple[List[VideoInQueue], Optional[str], int]:
        queue_list = [
            self.video_queue_store[vid]
            for vid in self.ordered_queue_ids
            if vid in self.video_queue_store
        ]
        return queue_list, self.current_video_id_in_queue, len(queue_list)

    def play_next_video(self) -> VideoInQueue:
        if not self.ordered_queue_ids:
            raise HTTPException(status_code=404, detail="Очередь пуста")

        current_id = self.current_video_id_in_queue
        if current_id is None:
            self.current_video_id_in_queue = self.ordered_queue_ids[0]
        else:
            try:
                current_index = self.ordered_queue_ids.index(current_id)
                if current_index < len(self.ordered_queue_ids) - 1:
                    self.current_video_id_in_queue = self.ordered_queue_ids[current_index + 1]
                else:
                    raise HTTPException(status_code=404, detail="Это конец очереди")
            except ValueError:
                self.current_video_id_in_queue = self.ordered_queue_ids[0]

        return self.get_video_entry(self.current_video_id_in_queue)

    def play_previous_video(self) -> VideoInQueue:
        if not self.ordered_queue_ids:
            raise HTTPException(status_code=404, detail="Очередь пуста")

        current_id = self.current_video_id_in_queue
        if current_id is None:
            raise HTTPException(status_code=404, detail="Видео не выбрано")

        try:
            current_index = self.ordered_queue_ids.index(current_id)
            if current_index > 0:
                self.current_video_id_in_queue = self.ordered_queue_ids[current_index - 1]
            else:
                raise HTTPException(status_code=404, detail="Это начало очереди")
        except ValueError:
            self.current_video_id_in_queue = self.ordered_queue_ids[0]

        return self.get_video_entry(self.current_video_id_in_queue)

    def get_current_video_details_for_stream(self) -> Tuple[Optional[str], str]:
        if not self.current_video_id_in_queue:
            return None, "Stream Offline"

        video_entry = self.get_video_entry(self.current_video_id_in_queue)

        if not video_entry:
            return None, "Stream Offline"
        
        # Главная логика: если видео скачано и путь существует, возвращаем локальный путь
        if video_entry.status == "downloaded" and video_entry.downloaded_path and os.path.exists(video_entry.downloaded_path):
            logger.info(f"Стриминг локального файла: {video_entry.downloaded_path}")
            return video_entry.downloaded_path, video_entry.title or "Downloaded Video"
        
        # Иначе, если есть URL, возвращаем его
        if video_entry.original_url and video_entry.status not in ["metadata_failed", "download_failed"]:
            logger.info(f"Стриминг URL: {video_entry.original_url}")
            return video_entry.original_url, video_entry.title or "Streaming Video"

        # Во всех остальных случаях (ошибка, нет URL) стримить нечего
        return None, "Stream Offline"

    def get_current_video_info_api(self) -> Optional[VideoInQueue]:
        if not self.current_video_id_in_queue:
            return None
        return self.get_video_entry(self.current_video_id_in_queue)

    def get_video_entry(self, video_id_in_queue: str) -> Optional[VideoInQueue]:
        return self.video_queue_store.get(video_id_in_queue)

    def initiate_download(
        self, video_id_in_queue: str, background_tasks: BackgroundTasks
    ) -> VideoInQueue:
        video_entry = self.get_video_entry(video_id_in_queue)
        if not video_entry:
            raise HTTPException(status_code=404, detail=f"Видео {video_id_in_queue} не найдено")

        if video_entry.status in ["downloaded", "downloading"]:
            return video_entry
        
        video_entry.status = "pending_download"
        background_tasks.add_task(self._download_video_task, video_id_in_queue)
        return video_entry

    def cancel_download(self, video_id_in_queue: str) -> VideoInQueue:
        video_entry = self.get_video_entry(video_id_in_queue)
        if not video_entry:
            raise HTTPException(status_code=404, detail=f"Видео {video_id_in_queue} не найдено")

        if video_entry.status in ["downloading", "pending_download"]:
            video_entry.status = "metadata_fetched"
            video_entry.error_message = "Загрузка отменена пользователем."
            logger.info(f"Загрузка '{video_entry.title}' отменена.")
        else:
            logger.warning(f"Нельзя отменить загрузку для '{video_entry.title}', статус: {video_entry.status}")
        return video_entry