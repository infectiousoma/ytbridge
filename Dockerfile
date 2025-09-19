FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn[standard] httpx redis python-multipart yt-dlp

COPY src/ ./src/

RUN mkdir -p /app/priv/data

EXPOSE 8080
ENV PORT=8080

CMD ["uvicorn", "src.ytbridge:app", "--host", "0.0.0.0", "--port", "8080"]
