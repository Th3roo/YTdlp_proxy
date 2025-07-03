import io
import time

from ..config import settings

from fastapi import FastAPI, Request, HTTPException, status, APIRouter
from fastapi.responses import StreamingResponse, FileResponse
import os

import logging

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/users/", tags=["users"])
async def read_users():
    return [{"username": "Rick"}, {"username": "Morty"}]


# Path to your video file
VIDEO_FILE_PATH = settings.get("video.dummy_video_path")
CHUNK_SIZE = 128  # 1MB chunks


class CustomIORawBase(io.RawIOBase):
    def __init__(self):
        self.real_file = open(VIDEO_FILE_PATH, 'rb')

    def read(self, size=-1):
        print("RETURN", self.real_file.tell())
        return self.real_file.read(CHUNK_SIZE)


@router.get("/test_video")
async def stream_video(request: Request):
    def iterfile():  # (1)
        with open(VIDEO_FILE_PATH, mode="rb") as file_like:  # (2)
            file_like.seek(0, io.SEEK_END)
            file_size = file_like.tell()
            file_like.seek(0)

            for _ in range(file_size):
                time.sleep(1)
                yield file_like.read(128)  # (3)

    print(request.headers)
    return StreamingResponse(iterfile(), media_type="video/mp4")
