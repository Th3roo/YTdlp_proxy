import pytest
from httpx import AsyncClient, ASGITransport # Add ASGITransport
from backend.main import app # Assuming 'app' is the FastAPI instance

import pytest_asyncio

# Mark all tests in this file as asyncio
pytestmark = pytest.mark.asyncio

@pytest_asyncio.fixture # Use pytest_asyncio.fixture for async fixtures
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

async def test_health_check(client: AsyncClient):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "Backend is running"}

async def test_stream_placeholder(client: AsyncClient):
    response = await client.get("/stream")
    assert response.status_code == 200
    assert response.json() == {"message": "Video stream placeholder. Actual stream to be implemented."}

async def test_play_pause_control(client: AsyncClient):
    # Initial state is playing=True
    response = await client.post("/control/play_pause")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "Stream paused"
    assert data["is_playing"] is False

    response = await client.post("/control/play_pause")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "Stream playing"
    assert data["is_playing"] is True

async def test_next_video_control(client: AsyncClient):
    # Reset state for this test if possible or make it independent
    # For simplicity, we assume it continues from previous tests or a fresh state if run independently
    # Let's assume initial index is 0 (or whatever it was left as)
    # To make it more robust, one might reset state in fixture or main.py for testing

    # Get initial index by calling next once (or have a dedicated state endpoint)
    initial_response = await client.post("/control/next") # e.g. index becomes 1 if initial was 0
    initial_index = initial_response.json()["current_video_index"]

    response = await client.post("/control/next")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "Next video requested"
    assert data["current_video_index"] == initial_index + 1 # It should increment

async def test_previous_video_control(client: AsyncClient):
    # Make sure index is at least 1 to test previous
    await client.post("/control/next") # index becomes 1 (assuming initial 0)
    await client.post("/control/next") # index becomes 2

    current_state_response = await client.post("/control/play_pause") # just to get current state via a side-effect
    current_index_before_prev = current_state_response.json().get("current_video_index", 2) # fallback if not in this response

    response = await client.post("/control/previous")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "Previous video requested"
    # This assertion depends on how many 'next' were called before in this session
    # For a more reliable test, we'd need to ensure current_video_index is > 0
    # or the backend should handle previous on index 0 gracefully.
    # Current backend logic: if index > 0, index -=1. So if it was 2, it becomes 1.
    assert data["current_video_index"] == current_index_before_prev -1


async def test_previous_video_control_at_zero(client: AsyncClient):
    # Reset index to 0 - this is tricky without a reset endpoint
    # For now, we'll assume we can get it to 0 by calling previous enough times
    # Or, we can rely on the fact that the `stream_state` is global and might be 0
    # Let's try to set it to 0 for the purpose of this test if the app object is directly available
    from backend.main import stream_state # direct import for test manipulation
    stream_state.current_video_index = 0

    response = await client.post("/control/previous")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "Previous video requested"
    assert data["current_video_index"] == 0 # Should not go below 0

    # Restore state if necessary, or ensure other tests don't depend on this mutation
    # stream_state.current_video_index = 0 # or some other default if needed
    # This direct manipulation is generally not ideal but useful for specific state testing without reset APIs.
    # A better way would be a /control/reset_state endpoint for testing.
    # For now, the tests for next/prev might be a bit flaky if run in a specific order without state reset.
    # The modification `stream_state.current_video_index = 0` will persist for the test session.
    # Consider adding a proper state reset API endpoint in the main app for more robust testing if needed.

# To run these tests:
# 1. Ensure backend/main.py uses `from backend.main import app` if tests are in `backend/tests/test_main.py`
#    and you run pytest from the root or `backend` directory.
#    If `app` is defined in `backend/main.py`, it might be `from main import app` if running from `backend/`
#    or `from backend.main import app` if running from root and `backend` is a package.
#    Let's assume `backend` is added to PYTHONPATH or tests are run such that `backend.main` is importable.
# 2. Add `httpx` and `pytest` and `pytest-asyncio` to backend requirements or a dev requirements file.
# Command: pytest
# For this to work, I'll adjust the import in `backend/tests/test_main.py`
# and add test dependencies to `backend/requirements.txt`.
