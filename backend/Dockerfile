FROM ghcr.io/astral-sh/uv:0.9.9 AS uv
FROM python:3.11.14-slim-bookworm
COPY --from=uv /uv /bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY app ./app
RUN uv sync --locked --no-dev --no-editable \
    && useradd --uid 10001 --create-home appuser
ENV PATH="/app/.venv/bin:$PATH"
USER appuser
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.api_app:app", "--host", "0.0.0.0", "--port", "8000"]
