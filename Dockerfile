FROM python:3.11

WORKDIR /app

COPY requirements.txt .
RUN apt update && \
    apt install ffmpeg -y && \
    pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "flask --app src.server:app run --host 0.0.0.0 --port ${PORT:-5000}"]

