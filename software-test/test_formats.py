import io
import json
import os
import re
import time
from pprint import pprint
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request, Header, Response
from fastapi.responses import StreamingResponse
import yt_dlp

resolution = 1080
ydl_opts = {
    # Options from your provided Python dict:
    "quiet": True,  # Corresponds to --quiet
    "noprogress": True,  # Corresponds to --no-progress
    'cookiesfrombrowser': ('firefox',),  # Corresponds to --cookies-from-browser firefox

    # Options translated from your original C# arguments:
    'nocheckcertificate': True,  # Corresponds to --no-check-certificate (Use with caution!)
    'no_cache_dir': True,  # Corresponds to --no-cache-dir
    'rm_cache_dir': True,  # Corresponds to --rm-cache-dir

    'format': f"(mp4+m4a/best)[height>=?{resolution}][height>=?64][protocol^=http]",
}

test_url = "https://www.youtube.com/watch?v=oOIztBXox60"

ydl = yt_dlp.YoutubeDL(ydl_opts)
info_dict: dict = ydl.extract_info(test_url, download=False)

list_formats = ydl.list_formats(info_dict)
print(json.dumps(list_formats))
print(json.dumps(info_dict))
