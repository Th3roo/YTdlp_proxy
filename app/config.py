# app/config.py

# Base options for yt-dlp.
# Specific functions might add or override these.
YDL_OPTS = {
    'quiet': True,              # Suppress yt-dlp output to stdout
    'noprogress': True,         # Do not print progress bar
    'noplaylist': True,         # Download only single video if URL refers to a video and a playlist
    'cookiesfrombrowser': ('firefox', None), # Try Firefox cookies, fallback to None if not found or error.
                                     # Use None as fallback to avoid errors if Firefox profile is not accessible.
    # 'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', # Generic format, often overridden
    # Consider adding common network options if needed, e.g.:
    # 'socket_timeout': 30, # seconds
    # 'retries': 5,
}