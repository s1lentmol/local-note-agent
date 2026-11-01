from contextlib import asynccontextmanager
from datetime import UTC
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from starlette.exceptions import HTTPException

from app.api.attachments import router as attachments_router
from app.api.notes import router as notes_router
from app.config import Settings
from app.db import create_sqlite_engine
from app.errors import api_error
from app.migrate import migrate
from app.models import WorkerState, utc_now
from app.schemas import (
    CapabilitiesResponse,
    ErrorResponse,
    HealthResponse,
    ReadinessResponse,
)


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > self.max_bytes:
            return await JSONResponse(
                {"error": {"code": "file_too_large", "message": "Превышен размер запроса."}},
                status_code=413,
            )(scope, receive, send)
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                raise api_error(413, "file_too_large", "Превышен размер запроса.")
            return message

        await self.app(scope, limited_receive, send)


def create_app(database_path: Path | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if database_path:
        settings = settings.model_copy(update={"data_dir": database_path.parent})
    database_path = database_path or settings.database_path

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_sqlite_engine(database_path)
        try:
            migrate(engine)
            app.state.session_factory = sessionmaker(engine)
            app.state.settings = settings
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="Local Note Agent",
        version="0.2.0",
        lifespan=lifespan,
        description=(
            "Бэкенд локальной базы заметок. Принимает текст, ссылки на HTML, изображения JPEG/PNG и аудио WAV/MP3. "
            "Хранит оригиналы, извлекает текст и ставит обогащение в очередь при подключённом адаптере.\n\n"
            "**Сценарий импорта:** POST /api/v1/notes или /notes/import → получить id → опрашивать GET /notes/{id} "
            "раз в 2 секунды. ingestion_status: queued → processing → ready/failed. "
            "enrichment_status независим: not_requested означает, что адаптер не включён. "
            "Ошибку можно повторить через /notes/{id}/retry.\n\n"
            "**Редактирование:** передайте expected_version из content_version. Конфликт — 409. "
            "Оригинал сохраняется, устаревшие результаты ИИ отбрасываются. Удаление скрывает заметку сразу, "
            "файлы и индекс очищает worker. Все даты — UTC.\n\n"
            "**Запуск из корня репозитория:** `python3 -m venv .venv`, "
            '` .venv/bin/python -m pip install -e "./backend[dev]"`. '
            "В двух терминалах: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1` "
            "и `.venv/bin/python -m app.worker`. Данные: backend/data; переопределение — NOTE_AGENT_DATA_DIR. "
            "Для OCR установите Tesseract с языками rus+eng. Для аудио один раз выполните "
            "`.venv/bin/python -m app.prepare_models` с доступом к сети. "
            "Далее распознавание выполняется локально.\n\n"
            "**Docker:** `docker compose up --build -d`. Сервис подготовки модели однократно загружает модель "
            "в общий volume. API доступен на 127.0.0.1:8000. GET /ready показывает состояние worker, "
            "GET /capabilities — лимиты.\n\n"
            "**Адаптер знаний:** NOTE_AGENT_KNOWLEDGE_ADAPTER=package.module:factory, фабрика принимает Settings "
            "и возвращает объект с upsert_note(NoteDocument) и delete_note(note_id). Типы находятся "
            "в app.integrations.knowledge. Вход: note_id, content_version, title, text. "
            "Ответ: note_id, content_version, summary, tags, category, entities (name/type). "
            "Повтор одной версии должен быть идемпотентным; delete_note — также. "
            "Адаптер устанавливается в окружение worker. После подключения POST /notes/reindex "
            "обрабатывает существующие заметки. Модуль поиска должен сверять note_id и content_version "
            "с GET заметки и исключать 404/устаревшие версии. Поиск, чат и граф реализуются отдельным модулем.\n\n"
            "**Проверка:** `.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests`. "
            "Миграции выполняются автоматически и сохраняют заметки базовой версии."
        ),
        openapi_tags=[
            {"name": "notes", "description": "Заметки, импорт и обработка."},
            {"name": "attachments", "description": "Скачивание оригиналов."},
            {"name": "health", "description": "Состояние сервера, worker и лимиты."},
        ],
        responses={503: {"model": ErrorResponse, "description": "Хранилище временно недоступно."}},
    )
    app.add_middleware(
        BodyLimitMiddleware,
        max_bytes=max(
            settings.audio_max_bytes, settings.image_max_bytes, settings.text_max_chars * 4
        )
        + 1024 * 1024,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )
    app.include_router(notes_router)
    app.include_router(attachments_router)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        error = (
            exc.detail
            if isinstance(exc.detail, dict)
            else {"code": "http_error", "message": str(exc.detail)}
        )
        return JSONResponse({"error": error}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "validation_error",
                    "message": "Проверьте поля запроса и их допустимые значения.",
                    "fields": [list(error["loc"]) for error in exc.errors()],
                }
            },
            status_code=422,
        )

    @app.exception_handler(OperationalError)
    async def database_error(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "database_unavailable",
                    "message": "Хранилище временно недоступно. Повторите запрос.",
                }
            },
            status_code=503,
        )

    @app.get(
        "/api/v1/health",
        tags=["health"],
        response_model=HealthResponse,
        summary="Проверить доступность API",
        description="Проверка процесса API без обращения к worker и моделям.",
    )
    def health():
        return {"status": "ok"}

    @app.get(
        "/api/v1/ready",
        tags=["health"],
        response_model=ReadinessResponse,
        summary="Проверить базу и worker",
        description="503 при недоступности базы или отсутствии heartbeat worker более 15 секунд. "
        "capabilities отдельно показывает доступность OCR, аудио и адаптера, не влияя на код HTTP.",
        responses={503: {"model": ReadinessResponse, "description": "База или worker не готовы."}},
    )
    def readiness(request: Request, response: Response):
        db_ok, worker_ok, available = False, False, {}
        try:
            with request.app.state.session_factory() as session:
                session.execute(text("SELECT 1"))
                db_ok = True
                worker = session.get(WorkerState, 1)
                if worker:
                    worker_ok = (
                        utc_now() - worker.heartbeat_at.replace(tzinfo=UTC)
                    ).total_seconds() < 15
                    available = worker.capabilities
        except OperationalError:
            pass
        ready = db_ok and worker_ok
        response.status_code = 200 if ready else 503
        return ReadinessResponse(
            status="ready" if ready else "degraded",
            database=db_ok,
            worker=worker_ok,
            capabilities=available,
        )

    @app.get(
        "/api/v1/capabilities",
        tags=["health"],
        response_model=CapabilitiesResponse,
        summary="Получить лимиты и поддерживаемые материалы",
        description="Лимиты настроены переменными NOTE_AGENT_*. Готовность OCR и аудиомодели проверяйте в GET /ready.",
    )
    def limits():
        return CapabilitiesResponse(
            source_types=["text", "url", "image", "audio"],
            max_image_bytes=settings.image_max_bytes,
            max_audio_bytes=settings.audio_max_bytes,
            max_audio_seconds=settings.audio_max_seconds,
            max_image_pixels=settings.image_max_pixels,
            max_text_chars=settings.text_max_chars,
            max_html_bytes=settings.html_max_bytes,
            knowledge_enabled=bool(settings.knowledge_adapter),
            polling_interval_seconds=2,
        )

    return app


app = create_app()
