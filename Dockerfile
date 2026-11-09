# The gateway image. One uvicorn process per container; run more containers to scale (docker-compose.yml).
FROM python:3.12.16-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first: this layer is cached and only rebuilt when requirements.txt changes, not on every code edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py config.yaml ./
COPY scripts/ scripts/

# Don't run as root: if someone breaks into the process, they don't get root in the container.
RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 8000
# The slim image has no curl, so use Python. Docker marks the container unhealthy if this fails.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

# Exec form (a JSON list), so uvicorn is process 1 and receives the SIGTERM from `docker stop` directly.
# It then stops taking new requests and finishes the ones in flight before exiting.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
