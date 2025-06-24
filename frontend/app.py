from flask import Flask, render_template
import os

app = Flask(__name__)

@app.route('/')
def index():
    # Get backend URL from environment variable, default to localhost for local dev
    backend_url = os.environ.get("BACKEND_URL", "http://localhost:8000")
    return render_template('index.html', backend_url=backend_url)

if __name__ == '__main__':
    # Debug mode should ideally be turned off for production containers
    # For this exercise, we'll leave it, but in a real prod deployment, set debug=False
    # or control it via an environment variable.
    app.run(host='0.0.0.0', port=5000, debug=True)
