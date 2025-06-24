# app/services.py

import logging
from typing import AsyncGenerator
from fastapi import HTTPException

from .streamer import YTDLSeekableStream
from .config import YDL_OPTS

CHUNK_SIZE = 1024 * 64  # 64 KB

async def stream_video_generator(video_id: str, start: int, end: int) -> AsyncGenerator[bytes, None]:
    """
    Асинхронный генератор, который создаёт, читает и закрывает стрим.
    Позволяет HTTPException из YTDLSeekableStream распространяться.
    Другие ошибки оборачивает в HTTPException.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    stream = None
    try:
        logging.info(f"Начало генерации потока для video_id: {video_id}, диапазон: {start}-{end}")
        stream = YTDLSeekableStream(url, YDL_OPTS)
        stream.seek(start) # Может вызвать HTTPException, если, например, extract_info не удался в __init__
        
        while (current_pos := stream._current_pos) <= end:
            read_size = min(CHUNK_SIZE, end - current_pos + 1)
            if read_size <= 0:
                logging.debug(f"Размер чтения {read_size} <= 0, завершение потока для {video_id}.")
                break
            
            data = stream.read(read_size) # Может вызвать HTTPException
            if not data:
                logging.debug(f"stream.read() не вернул данных, завершение потока для {video_id}.")
                break
            
            yield data
        logging.info(f"Завершение генерации потока для video_id: {video_id}")

    except HTTPException:
        # Позволяем HTTPException распространяться без изменений
        logging.error(f"HTTPException в генераторе стрима для video_id: {video_id}")
        raise
    except Exception as e:
        # Оборачиваем другие ошибки в HTTPException
        logging.error(f"Неожиданная ошибка в генераторе стрима для video_id: {video_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка сервера при генерации потока: {str(e)}")
    finally:
        if stream:
            logging.info(f"Закрытие потока для video_id: {video_id}")
            stream.close() # close() также имеет свою обработку ошибок

def get_video_details(video_id: str) -> tuple[int, str]:
    """
    Получает метаданные видео (размер, MIME-тип) без создания полного потока.
    YTDLSeekableStream.__init__ может вызвать HTTPException, которая будет распространяться.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    stream_info = None
    try:
        logging.info(f"Получение деталей для video_id: {video_id}")
        # Используем временный экземпляр для получения информации
        stream_info = YTDLSeekableStream(url, YDL_OPTS) # Может вызвать HTTPException
        total_size = stream_info.total_size
        mime_type = f"video/{stream_info._format.get('ext', 'mp4')}"
        logging.info(f"Детали для video_id: {video_id}: размер={total_size}, тип={mime_type}")
        return total_size, mime_type
    except HTTPException:
        logging.error(f"HTTPException при получении деталей для video_id: {video_id}")
        raise # Распространяем HTTPException дальше
    except Exception as e:
        logging.error(f"Неожиданная ошибка при получении деталей для video_id: {video_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Не удалось получить детали видео: {str(e)}")
    finally:
        if stream_info:
            logging.info(f"Закрытие stream_info для video_id: {video_id}")
            stream_info.close() # close() также имеет свою обработку ошибок