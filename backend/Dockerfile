FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NOTE_AGENT_DATA_DIR=/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY app ./app
RUN pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home appuser \
    && mkdir -p /data && chown appuser:appuser /data
USER appuser
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
