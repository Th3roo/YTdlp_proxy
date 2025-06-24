# app/services.py

import logging
from typing import AsyncGenerator

from .streamer import YTDLSeekableStream
from .config import YDL_OPTS

CHUNK_SIZE = 1024 * 64  # 64 KB

async def stream_video_generator(video_id: str, start: int, end: int) -> AsyncGenerator[bytes, None]:
    """
    Асинхронный генератор, который создаёт, читает и закрывает стрим.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    stream = None
    try:
        stream = YTDLSeekableStream(url, YDL_OPTS)
        stream.seek(start)
        
        while (current_pos := stream._current_pos) <= end:
            # Определяем, сколько байт читать
            read_size = min(CHUNK_SIZE, end - current_pos + 1)
            if read_size <= 0:
                break
            
            data = stream.read(read_size)
            if not data:
                break
            
            yield data

    except Exception as e:
        logging.error(f"Ошибка в генераторе стрима: {e}")
        # Можно пробросить ошибку дальше, если нужно
    finally:
        if stream:
            stream.close()

def get_video_details(video_id: str) -> tuple[int, str]:
    """
    Получает метаданные видео (размер, MIME-тип) без создания полного потока.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    # Используем временный экземпляр для получения информации
    stream_info = YTDLSeekableStream(url, YDL_OPTS)
    total_size = stream_info.total_size
    mime_type = f"video/{stream_info._format.get('ext', 'mp4')}"
    # Важно! Закрываем сразу, чтобы не оставлять мусор.
    stream_info.close()
    return total_size, mime_type