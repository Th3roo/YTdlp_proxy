# main.py

from fastapi import FastAPI
from app.api import router as api_router

# Создание экземпляра FastAPI
app = FastAPI(
    title="Video Streaming Proxy",
    description="Прокси-сервис для потоковой передачи видео с поддержкой перемотки.",
    version="1.0.0",
)

# Добавляем маршруты из нашего API
app.include_router(api_router)

@app.get("/", tags=["Root"])
def read_root():
    """
    Корневой эндпоинт для проверки работоспособности сервиса.
    """
    return {"message": "Сервис потокового видео запущен!"}