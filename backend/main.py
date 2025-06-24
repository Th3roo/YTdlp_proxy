from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import logging

app = FastAPI()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory state (very basic for now)
class StreamState(BaseModel):
    is_playing: bool = True
    current_video_index: int = 0 # Placeholder

stream_state = StreamState()

@app.get("/health")
async def health_check():
    return {"status": "ok", "message": "Backend is running"}

@app.get("/stream")
async def stream_video():
    # Placeholder for actual video streaming logic
    # For yt-dlp to work, this endpoint will eventually need to stream video data
    # and set appropriate Content-Type headers.
    logger.info("Stream endpoint accessed")
    return {"message": "Video stream placeholder. Actual stream to be implemented."}

@app.post("/control/play_pause")
async def play_pause_control():
    stream_state.is_playing = not stream_state.is_playing
    action = "Playing" if stream_state.is_playing else "Paused"
    logger.info(f"Control: Play/Pause toggled. Stream is now {action.lower()}. Current index: {stream_state.current_video_index}")
    return {
        "status": f"Stream {action.lower()}",
        "is_playing": stream_state.is_playing,
        "current_video_index": stream_state.current_video_index  # Add current_video_index to response
    }

@app.post("/control/next")
async def next_video_control():
    stream_state.current_video_index += 1
    logger.info(f"Control: Next video. Current index: {stream_state.current_video_index}")
    # Actual logic to change video source will be here
    return {"status": "Next video requested", "current_video_index": stream_state.current_video_index}

@app.post("/control/previous")
async def previous_video_control():
    if stream_state.current_video_index > 0:
        stream_state.current_video_index -= 1
    logger.info(f"Control: Previous video. Current index: {stream_state.current_video_index}")
    # Actual logic to change video source will be here
    return {"status": "Previous video requested", "current_video_index": stream_state.current_video_index}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
