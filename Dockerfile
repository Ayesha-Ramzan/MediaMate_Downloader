# Use Python 3.11 slim image
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies required by yt-dlp and ffmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    ffprobe \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY app.py .

# Create persistent volume mount point
RUN mkdir -p /mnt/data

# Expose port (Render will set PORT env var)
EXPOSE 8000

# Run the app
CMD ["python", "app.py"]
