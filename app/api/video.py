import asyncio
import logging
import os
from typing import Annotated, Optional

import yt_dlp
from fastapi import (APIRouter, BackgroundTasks, Body, Header, HTTPException,
                     Request)
from fastapi.responses import StreamingResponse, FileResponse

from app.config import YDL_OPTS, VIDEO_CACHE_DIR, CHUNK_DURATION_SECONDS
# ИЗМЕНЕНО: get_video_stream_urls больше не нужен для этой логики
from app.core.ytdlp import download_video, get_video_info
from app.models.video import (ActionSuccessResponse, AddVideoResponse,
                              CurrentVideoResponse, ErrorResponse, QueueResponse,
                              VideoCreate)
from app.queue_manager import VideoQueueManager
from app.streaming import YTDLPSeekableStream, parse_range_header
# ИЗМЕНЕНО: импортируем новую, правильную функцию
from app.processing import download_and_cut_segment


router = APIRouter()
queue_manager = VideoQueueManager()
logger = logging.getLogger(__name__)

PLACEHOLDER_VIDEO_PATH = "static/offline_video.mp4"
CHUNK_SIZE = 64 * 1024

os.makedirs(VIDEO_CACHE_DIR, exist_ok=True)


async def read_file_chunked(file_path: str, start: int, end: int):
    loop = asyncio.get_event_loop()
    try:
        f = await loop.run_in_executor(None, open, file_path, "rb")
        await loop.run_in_executor(None, f.seek, start)
        bytes_to_read = end - start + 1
        try:
            while bytes_to_read > 0:
                chunk = await loop.run_in_executor(None, f.read, min(CHUNK_SIZE, bytes_to_read))
                if not chunk: break
                yield chunk
                bytes_to_read -= len(chunk)
        finally:
            await loop.run_in_executor(None, f.close)
    except Exception as e:
        logger.error(f"Ошибка при чтении файла {file_path}: {e}", exc_info=True)
        return

ext_to_mime = { "mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska", "mov": "video/quicktime" }


@router.get("/live_stream")
async def stream_live_video(request: Request, range_header: Optional[str] = Header(None, alias="Range")):
    active_video_url, active_video_title = queue_manager.get_current_video_details_for_stream()

    if active_video_url and os.path.exists(active_video_url):
        try:
            file_size = os.path.getsize(active_video_url)
            start_byte, end_byte = 0, file_size - 1
            status_code = 200
            headers = { "Accept-Ranges": "bytes", "Content-Type": "video/mp4", "X-Stream-Title": active_video_title.encode('utf-8', 'surrogateescape').decode('latin-1', 'replace'), "Content-Length": str(file_size) }
            if range_header:
                start_byte, end_byte = parse_range_header(range_header, file_size)
                status_code = 206
                headers["Content-Range"] = f"bytes {start_byte}-{end_byte}/{file_size}"
                headers["Content-Length"] = str(end_byte - start_byte + 1)
            return StreamingResponse(read_file_chunked(active_video_url, start_byte, end_byte), status_code=status_code, headers=headers)
        except Exception as e:
            logger.error(f"Ошибка стриминга локального файла {active_video_url}: {e}", exc_info=True)
            active_video_url = None
    
    elif active_video_url:
        stream = None
        try:
            stream = YTDLPSeekableStream(url=active_video_url, ydl_opts=YDL_OPTS.copy())
            total_size = stream.total_size
            content_type = "video/mp4"
            if stream.format:
                mime = stream.format.get("mime_type") or ext_to_mime.get(stream.format.get("ext"))
                if mime: content_type = mime
            headers = { "Accept-Ranges": "bytes", "Content-Type": content_type, "X-Stream-Title": active_video_title.encode('utf-8', 'surrogateescape').decode('latin-1', 'replace'), }
            start_byte, end_byte = 0, (total_size - 1) if total_size else None
            status_code = 200
            if range_header and total_size:
                start_byte, end_byte = parse_range_header(range_header, total_size)
                await stream.seek(start_byte)
                headers["Content-Range"] = f"bytes {start_byte}-{end_byte}/{total_size}"
                status_code = 206
            content_length = (end_byte - start_byte + 1) if end_byte is not None else total_size
            if content_length is not None: headers["Content-Length"] = str(content_length)
            async def content_iterator():
                try:
                    remaining_bytes = content_length
                    while remaining_bytes is None or remaining_bytes > 0:
                        read_size = CHUNK_SIZE if remaining_bytes is None else min(CHUNK_SIZE, remaining_bytes)
                        chunk = await stream.read(read_size)
                        if not chunk: break
                        yield chunk
                        if remaining_bytes is not None: remaining_bytes -= len(chunk)
                finally:
                    if stream: await stream.close()
            return StreamingResponse(content_iterator(), status_code=status_code, headers=headers)
        except Exception as e:
            if stream: await stream.close()
            logger.error(f"Ошибка стриминга URL {active_video_url}: {e}", exc_info=True)
            
    if os.path.exists(PLACEHOLDER_VIDEO_PATH):
        file_size = os.path.getsize(PLACEHOLDER_VIDEO_PATH)
        start_byte, end_byte = 0, file_size - 1
        status_code = 200
        headers = { "Accept-Ranges": "bytes", "Content-Type": "video/mp4", "X-Stream-Title": "Stream Offline", "Content-Length": str(file_size) }
        if range_header:
            start_byte, end_byte = parse_range_header(range_header, file_size)
            status_code = 206
            headers["Content-Range"] = f"bytes {start_byte}-{end_byte}/{file_size}"
            headers["Content-Length"] = str(end_byte - start_byte + 1)
        return StreamingResponse(read_file_chunked(PLACEHOLDER_VIDEO_PATH, start_byte, end_byte), status_code=status_code, headers=headers)
    
    raise HTTPException(status_code=500, detail="Ни один источник видео не доступен, и файл-заглушка не найден.")


@router.get("/stream_remux/{video_id}")
async def stream_video_remux(
    video_id: str,
    request: Request,
    chunk: int = 0
):
    """
    Streams video by creating segments on the fly using the robust yt-dlp method.
    """
    # ИЗМЕНЕНО: Нам больше не нужна информация о потоках, мы передаем URL напрямую.
    youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    
    start_time = chunk * CHUNK_DURATION_SECONDS
    video_cache_path = os.path.join(VIDEO_CACHE_DIR, video_id)
    os.makedirs(video_cache_path, exist_ok=True)
    
    chunk_filename = f"chunk_{chunk:04d}.mp4"
    chunk_filepath = os.path.join(video_cache_path, chunk_filename)
    chunk_wip_filepath = f"{chunk_filepath}.write.mp4" # yt-dlp создаст этот файл

    if not os.path.exists(chunk_filepath):
        if os.path.exists(chunk_wip_filepath):
            for _ in range(60): # Увеличим ожидание до 60с, т.к. скачивание может быть долгим
                await asyncio.sleep(1)
                if os.path.exists(chunk_filepath): break
            else:
                raise HTTPException(status_code=500, detail="Timed out waiting for video chunk to be generated.")
        else:
            logger.info(f"Chunk {chunk_filename} not found. Generating with yt-dlp...")
            loop = asyncio.get_event_loop()
            # ИЗМЕНЕНО: Вызываем новую, надежную функцию
            success = await loop.run_in_executor(
                None,
                download_and_cut_segment,
                youtube_url, # Передаем оригинальный URL
                chunk_filepath,
                start_time,
                CHUNK_DURATION_SECONDS
            )
            if not success:
                raise HTTPException(status_code=500, detail="Failed to process video chunk with yt-dlp.")
            
    if os.path.exists(chunk_filepath):
        return FileResponse(
            chunk_filepath,
            media_type="video/mp4",
            filename=f"{video_id}_{chunk_filename}"
        )
    else:
        raise HTTPException(status_code=404, detail="Chunk file not found after processing.")


@router.get("/stream/{video_id}")
async def stream_video(video_id: str, request: Request, range: Annotated[str | None, Header()] = None):
    url = f"https://www.youtube.com/watch?v={video_id}"
    stream = None
    try:
        stream = YTDLPSeekableStream(url, ydl_opts=YDL_OPTS.copy())
        total_size = stream.total_size
        content_type = "video/mp4"
        if stream.format and stream.format.get("ext"):
            content_type = ext_to_mime.get(stream.format.get("ext"), "video/mp4")
        headers = {"Accept-Ranges": "bytes", "Content-Type": content_type}
        start_byte, end_byte = 0, (total_size - 1) if total_size else None
        status_code = 200
        if range and total_size:
            start_byte, end_byte = parse_range_header(range, total_size)
            await stream.seek(start_byte)
            headers["Content-Range"] = f"bytes {start_byte}-{end_byte}/{total_size}"
            status_code = 206
        content_length = (end_byte - start_byte + 1) if end_byte is not None else total_size
        if content_length is not None: headers["Content-Length"] = str(content_length)
        async def iter_content():
            try:
                remaining = content_length
                while remaining is None or remaining > 0:
                    read_size = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
                    data = await stream.read(read_size)
                    if not data: break
                    yield data
                    if remaining: remaining -= len(data)
            finally:
                if stream: await stream.close()
        return StreamingResponse(iter_content(), status_code=status_code, headers=headers)
    except Exception as e:
        if stream: await stream.close()
        logger.error(f"Ошибка прямого стриминга {video_id}: {e}", exc_info=True)
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=f"Неожиданная ошибка при стриминге: {e}")


@router.post("/video/add", response_model=AddVideoResponse, status_code=200)
async def add_video_to_queue(payload: VideoCreate = Body(...)):
    try:
        new_video_entry, position = await queue_manager.add_video(payload)
    except Exception as e:
        logger.error(f"Ошибка при добавлении видео в очередь: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Внутренняя ошибка при добавлении видео.")
    if new_video_entry.status == "downloaded": message = "Видео найдено локально и добавлено в очередь."
    elif new_video_entry.status == "metadata_fetched": message = "Метаданные получены, видео готово к скачиванию."
    else: message = "Не удалось обработать видео. Проверьте статус в очереди."
    return AddVideoResponse(message=message, video_info=new_video_entry, queue_position=position)


@router.get("/queue", response_model=QueueResponse)
async def get_queue_state():
    queue_list, current_id, total_items = queue_manager.get_queue_state()
    return QueueResponse(queue=queue_list, current_video_id_in_queue=current_id, total_items=total_items)


@router.post("/video/play_next", response_model=ActionSuccessResponse, responses={404: {"model": ErrorResponse}},)
async def play_next_video():
    current_video = queue_manager.play_next_video()
    return ActionSuccessResponse(message="Воспроизводится следующее видео", current_video=current_video)


@router.post("/video/play_previous", response_model=ActionSuccessResponse, responses={404: {"model": ErrorResponse}},)
async def play_previous_video():
    current_video = queue_manager.play_previous_video()
    return ActionSuccessResponse(message="Воспроизводится предыдущее видео", current_video=current_video)


@router.get("/current_video_link", response_model=CurrentVideoResponse, responses={404: {"model": ErrorResponse}},)
async def get_current_video_details():
    current_video = queue_manager.get_current_video_info_api()
    if current_video: return CurrentVideoResponse(message="Информация о текущем видео.", video_info=current_video)
    return CurrentVideoResponse(message="Очередь пуста или видео не выбрано.", video_info=None)

@router.post("/video/{video_id_in_queue}/download", response_model=ActionSuccessResponse, status_code=202, responses={404: {"model": ErrorResponse}},)
async def download_single_video(video_id_in_queue: str, background_tasks: BackgroundTasks):
    updated_video_entry = queue_manager.initiate_download(video_id_in_queue, background_tasks)
    if updated_video_entry.status == "downloaded": message = f"Видео '{updated_video_entry.title}' уже скачано."
    elif updated_video_entry.status == "downloading": message = f"Видео '{updated_video_entry.title}' уже скачивается."
    else: message = f"Начата загрузка видео '{updated_video_entry.title}'."
    return ActionSuccessResponse(message=message, current_video=updated_video_entry)

@router.post("/video/{video_id_in_queue}/cancel_download", response_model=ActionSuccessResponse, responses={404: {"model": ErrorResponse}},)
async def cancel_video_download(video_id_in_queue: str):
    updated_video_entry = queue_manager.cancel_download(video_id_in_queue)
    return ActionSuccessResponse(message="Запрос на отмену обработан.", current_video=updated_video_entry)