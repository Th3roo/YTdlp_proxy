from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.api import video

app = FastAPI(title="Stream Control API")

# Подключаем роутеры API
app.include_router(video.router, prefix="/api/v1", tags=["video"])

# Обслуживание статических файлов для фронтенда
app.mount("/", StaticFiles(directory="static", html=True), name="static")

@app.get("/health", tags=["healthcheck"])
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
