FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN ffmpeg -version && ffprobe -version

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p /recordings

RUN chmod +x entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; r = httpx.get('http://localhost:8000/health'); r.raise_for_status()"

CMD ["./entrypoint.sh"]
