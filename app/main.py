from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.api import video

app = FastAPI(title="Stream Control API")

app.include_router(video.router, prefix="/api/v1", tags=["video"])


@app.get("/health", tags=["healthcheck"])
async def health_check():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
