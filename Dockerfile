FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cache bust - increment to force fresh COPY of app files
ARG CACHEBUST=1

# Copy application code
COPY . .

# Expose port
EXPOSE 8080

# Run the app
CMD ["python", "app.py"]
