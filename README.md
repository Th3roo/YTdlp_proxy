# Stream Control Web Interface

This project provides a web interface to control a video stream, with a backend built using FastAPI and a frontend using Flask. The entire application is designed to be run with Docker Compose.

## Project Structure

-   `backend/`: Contains the FastAPI application.
    -   `main.py`: The main FastAPI application logic, including stream controls and a placeholder stream endpoint.
    -   `Dockerfile`: Dockerfile for building the backend service.
    -   `requirements.txt`: Python dependencies for the backend.
    -   `tests/`: Pytest tests for the backend.
-   `frontend/`: Contains the Flask application that serves the web interface.
    -   `app.py`: The main Flask application logic.
    -   `templates/index.html`: The HTML/JavaScript for the user interface.
    -   `Dockerfile`: Dockerfile for building the frontend service.
    -   `requirements.txt`: Python dependencies for the frontend.
    -   `tests/`: Pytest tests for the frontend.
-   `docker-compose.yml`: Defines how to run the backend and frontend services together.
-   `README.md`: This file.

## Prerequisites

-   Docker
-   Docker Compose

## Running the Application

1.  **Clone the repository** (if applicable) or ensure all files are in place.

2.  **Build and run the services using Docker Compose:**
    Open a terminal in the root directory of the project (where `docker-compose.yml` is located) and run:
    ```bash
    docker-compose up --build
    ```
    This command will:
    -   Build the Docker images for both the `backend` and `frontend` services if they don't exist or if their Dockerfiles have changed.
    -   Start the containers for both services.

3.  **Access the web interface:**
    Once the containers are running, open your web browser and go to:
    [http://localhost:5000/](http://localhost:5000/)

    You should see the stream control interface.

4.  **Access the backend health check and stream placeholder:**
    -   Backend Health: [http://localhost:8000/health](http://localhost:8000/health)
    -   Stream Placeholder: [http://localhost:8000/stream](http://localhost:8000/stream) (This is a placeholder and does not stream actual video yet). Clients like `yt-dlp` can attempt to access this URL.

## Development

### Running Tests

**Backend Tests:**
Make sure you have installed dependencies from `backend/requirements.txt` (including test dependencies).
Navigate to the `backend` directory or run from root:
```bash
python -m pytest backend/tests/test_main.py
```
(If you have `backend` in your `PYTHONPATH` or are using an IDE that handles it, `pytest backend` might also work from the root).

**Frontend Tests:**
Make sure you have installed dependencies from `frontend/requirements.txt` (including test dependencies).
Navigate to the `frontend` directory or run from root:
```bash
python -m pytest frontend/tests/test_app.py
```

### Local Development (without Docker)

1.  **Backend:**
    -   Navigate to `backend/`.
    -   Create a virtual environment and install `requirements.txt`.
    -   Run: `uvicorn main:app --reload --port 8000`
2.  **Frontend:**
    -   Navigate to `frontend/`.
    -   Create a virtual environment and install `requirements.txt`.
    -   Run: `python app.py` (or `flask run --debug`)
    -   The frontend will run on `http://localhost:5000` and attempt to connect to the backend at `http://localhost:8000` by default.

## API Endpoints (Backend - FastAPI)

The backend runs on port 8000 (inside Docker, `http://backend:8000`; exposed as `http://localhost:8000` on the host).

-   `GET /health`: Returns the health status of the backend.
    -   Response: `{"status": "ok", "message": "Backend is running"}`
-   `GET /stream`: Placeholder for the video stream.
    -   Response: `{"message": "Video stream placeholder. Actual stream to be implemented."}`
-   `POST /control/play_pause`: Toggles the play/pause state of the stream.
    -   Response: `{"status": "Stream playing/paused", "is_playing": true/false, "current_video_index": N}`
-   `POST /control/next`: Requests the next video in the (conceptual) playlist.
    -   Response: `{"status": "Next video requested", "current_video_index": N}`
-   `POST /control/previous`: Requests the previous video in the (conceptual) playlist.
    -   Response: `{"status": "Previous video requested", "current_video_index": N}`

## Future Enhancements (Not Implemented)

-   Actual video streaming from the `/stream` endpoint.
-   Loading video files from disk and managing a playlist.
-   More robust state management.
-   User authentication/authorization if needed.
-   More sophisticated frontend (e.g., using a JavaScript framework).
-   Production-grade WSGI/ASGI servers in Docker images (e.g., Gunicorn).
```
