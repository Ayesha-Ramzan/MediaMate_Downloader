# Use the official lightweight Python image
FROM python:3.10-slim

# Install system dependencies (FFmpeg required by your backend)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /code

# Copy requirements and install Python dependencies
COPY ./requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

# Copy the rest of the application code
COPY . .

# Hugging Face Spaces listens on port 7860 by default
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]