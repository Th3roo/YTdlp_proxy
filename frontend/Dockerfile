# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app/frontend

# Copy the requirements file into the container at /app/frontend
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the frontend application code into the container at /app/frontend
# This includes app.py and the templates directory
COPY . .

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Define environment variable
ENV FLASK_APP="app.py"
ENV FLASK_RUN_HOST="0.0.0.0"
ENV FLASK_RUN_PORT="5000"
# ENV FLASK_DEBUG=1 # Uncomment for development debugging

# Run app.py when the container launches using Flask CLI
# Using 'flask run' is common for development, but for production gunicorn is often preferred.
# For simplicity here, we'll stick to 'flask run'.
# The app.py itself has `app.run(host='0.0.0.0', port=5000)` which is also fine.
# Using CMD ["python", "app.py"] will execute the if __name__ == '__main__': block.
CMD ["python", "app.py"]
