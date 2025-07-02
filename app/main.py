import logging
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.api import video

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Stream Control API")

app.include_router(video.router, prefix="/api/v1", tags=["video"])


@app.get("/health", tags=["healthcheck"])
async def health_check():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
