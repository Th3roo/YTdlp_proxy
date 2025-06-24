# app/config.py

# Опции для yt-dlp. Мы используем cookies из Firefox для доступа
# к видео, требующим авторизации.
YDL_OPTS = {
    'quiet': True,
    'noprogress': True,
    'cookiesfrombrowser': ('firefox',),
    # 'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
}