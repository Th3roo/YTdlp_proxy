# app/config.py

# Base options for yt-dlp.
# Specific functions might add or override these.
YDL_OPTS = {
    "quiet": True,  # Suppress yt-dlp output to stdout
    "noprogress": True,  # Do not print progress bar
    "noplaylist": True,  # Download only single video if URL refers to a video and a playlist
    "no_cookies_from_browser": True,  # Do not attempt to load cookies from browser
    # 'cookiesfrombrowser': ('firefox', None), # Removed this line
    "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",  # Возвращаем универсальный формат
    # Consider adding common network options if needed, e.g.:
    # 'socket_timeout': 30, # seconds
    # 'retries': 5,
}
