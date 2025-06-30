import yt_dlp
import json # Import the json library for pretty printing

def list_video_formats(url):
    """
    Lists the available formats for a given video URL using yt-dlp.

    Args:
        url (str): The URL of the video.
    """
    ydl_opts = {
        'listformats': True, # Although extract_info with download=False is used, this option is relevant for understanding format listing. [3]
        'format': 'bestvideo+bestaudio/best', # Specify a default format
        'noplaylist': True, # Do not download a playlist if the URL is part of one
        'verbose': True, # Enable verbose output for more details
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract information without downloading the video
            info_dict = ydl.extract_info(url, download=False) [8, 10]

            print(f"Available formats for video: {info_dict.get('title', 'N/A')}")
            print("-" * 50)

            # The 'formats' key contains the list of available formats [10]
            formats = info_dict.get('formats')

            if formats:
                # Print header for formats
                print(f"{'Format Code':<15} {'Extension':<10} {'Resolution':<15} {'Note':<50}")
                print("-" * 100)

                # Iterate through the formats and print details
                for f in formats:
                    format_id = f.get('format_id', 'N/A')
                    ext = f.get('ext', 'N/A')
                    resolution = f.get('resolution', 'N/A')
                    note = f.get('format_note', 'N/A')
                    print(f"{format_id:<15} {ext:<10} {resolution:<15} {note:<50}")
            else:
                print("No formats found for this video.")

    except Exception as e:
        print(f"An error occurred: {e}")

# --- Demo Usage ---
if __name__ == "__main__":
    video_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # Example YouTube URL
    list_video_formats(video_url)