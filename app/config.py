
YDL_OPTS = {
    'quiet': True,
    'noprogress': True,
    'noplaylist': True,
    'no_cookies_from_browser': False,
    'cookiesfrombrowser': ('firefox', None), # Removed this line
    'format': 'bestvideo+bestaudio/best',
    'outtmpl': 'downloads/%(title)s [%(id)s].%(ext)s',
}

STREAM_EXTRACT_OPTS = {
    'quiet': True,
    'noprogress': True,
    'noplaylist': True,
    'no_cookies_from_browser': True,
    'format': 'bestvideo/bestvideo,bestaudio/bestaudio',
}

CHUNK_DURATION_SECONDS = 10
VIDEO_CACHE_DIR = "video_cache"