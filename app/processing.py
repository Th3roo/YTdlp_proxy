import logging
import os
import yt_dlp

logger = logging.getLogger(__name__)

def download_and_cut_segment(
    video_url: str,
    output_path: str,
    start_time: float,
    duration: float
) -> bool:
    """
    Uses yt-dlp to download and cut a specific segment of a video.
    This is the most robust method as it delegates all complex processing
    (handling manifests, merging, cutting) to yt-dlp and its FFmpeg backend.

    This is a blocking function and should be run in an executor.

    Args:
        video_url: The original URL of the video (e.g., youtube.com/watch?v=...).
        output_path: The path to save the final MP4 segment.
        start_time: The start time of the segment in seconds.
        duration: The duration of the segment to process.

    Returns:
        True if successful, False otherwise.
    """
    logger.info(f"Starting segment download for '{os.path.basename(output_path)}' from {start_time:.2f}s.")
    
    output_temp_path = f"{output_path}.write"
    
    # Конфигурация для yt-dlp, чтобы скачать и нарезать сегмент
    ydl_opts = {
        'quiet': True,
        'noprogress': True,
        'noplaylist': True,
        # Самый надежный селектор: лучшее видео + лучшее аудио, затем лучший объединенный
        'format': 'bestvideo+bestaudio/best',
        # Ключевая опция: говорим yt-dlp скачать только нужный временной диапазон
        'download_sections': f"*{start_time}-{start_time + duration}",
        # Принудительно используем ffmpeg для нарезки, т.к. это самый точный способ
        'force_keyframes_at_cuts': True,
        # Указываем, куда сохранять временный и конечный файлы
        'outtmpl': output_temp_path,
        # Говорим yt-dlp, что после сборки формат должен быть mp4
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # yt-dlp сам обработает URL, скачает, нарежет и сохранит результат
            error_code = ydl.download([video_url])
            if error_code != 0:
                logger.error(f"yt-dlp returned a non-zero exit code {error_code} for segment download.")
                return False
        
        # yt-dlp после postprocessing'а может поменять расширение. 
        # Нам нужно найти конечный файл. Обычно это .mp4
        final_file_path = f"{output_temp_path}.mp4"
        if not os.path.exists(final_file_path):
             final_file_path = output_temp_path # Если конвертации не было
             if not os.path.exists(final_file_path):
                logger.error(f"Could not find the final output file after yt-dlp processing for {output_path}")
                return False

        # Переименовываем в финальное имя, убирая .write
        os.rename(final_file_path, output_path)
        
        logger.info(f"Successfully processed segment to {os.path.basename(output_path)}")
        return True

    except Exception as e:
        logger.error(f"Exception during yt-dlp segment processing for {output_path}: {e}", exc_info=True)
        if os.path.exists(output_temp_path):
            # Подчищаем мусор, если он остался
            if os.path.exists(f"{output_temp_path}.mp4"): os.remove(f"{output_temp_path}.mp4")
            if os.path.exists(output_temp_path): os.remove(output_temp_path)
        return False