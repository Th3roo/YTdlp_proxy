# app/api.py

from typing import Annotated
from fastapi import APIRouter, Header, Request, HTTPException
from fastapi.responses import StreamingResponse

from .services import get_video_details, stream_video_generator
from .utils import parse_range_header

# Создаем маршрутизатор
router = APIRouter()

@router.get("/stream/{video_id}")
async def stream_video(
    video_id: str,
    request: Request,
    range: Annotated[str | None, Header()] = None,
):
    try:
        total_size, media_type = get_video_details(video_id)
        start, end = parse_range_header(range, total_size)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    content_length = end - start + 1
    
    # Создаем генератор для потоковой передачи
    video_generator = stream_video_generator(video_id, start, end)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": media_type,
        "Content-Length": str(content_length),
    }
    
    status_code = 200
    if range:
        status_code = 206  # Partial Content
        headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"

    return StreamingResponse(
        video_generator,
        status_code=status_code,
        headers=headers,
    )