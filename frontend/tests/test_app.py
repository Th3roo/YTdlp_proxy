import pytest
from frontend.app import app as flask_app # Alias to avoid confusion

@pytest.fixture
def client():
    flask_app.config['TESTING'] = True
    # Ensure backend_url is set for testing, even if it's a dummy one,
    # as the template expects it.
    # If your app context or setup normally handles this, adjust accordingly.
    # For this test, we just need the route to render without error.
    with flask_app.test_client() as client:
        yield client

def test_index_page_loads(client):
    """Test that the index page loads and contains some expected content."""
    response = client.get('/')
    assert response.status_code == 200
    assert b"<h1>Stream Controls</h1>" in response.data
    assert b'<button id="playPauseBtn">Play/Pause</button>' in response.data
    assert b'href="http://localhost:8000/stream"' in response.data # Check default backend_url

# To run these tests:
# 1. Ensure frontend/app.py is importable.
# 2. Add pytest to frontend requirements or a dev requirements file.
# Command: pytest (run from the 'frontend' directory or configure pytest paths)
