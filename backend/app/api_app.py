"""API entrypoint for the PostgreSQL deployment."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException

from app.api.chat import router as chat_router
from app.api.collections import router as collection_router
from app.api.file_import import router as file_router
from app.api.graph import router as graph_router
from app.api.imports import router as import_router
from app.api.note_crud import router
from app.api.search import router as search_router
from app.config import Settings
from app.dto import ErrorResponse
from app.errors import ProcessingError
from app.postgres import Database
from app.services.chat import ChatError, ChatService
from app.services.collections import CollectionsService
from app.services.file_import import FileImportService
from app.services.graph import GraphService
from app.services.imports import ImportsService
from app.services.notes import NoteError, NotesService
from app.services.search import SearchError, SearchService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        database = Database(settings.database_url) if settings.database_url else None
        app.state.files = None
        app.state.imports = None
        app.state.collections = None
        app.state.notes = None
        app.state.graph = None
        app.state.search = None
        app.state.chat = None
        try:
            if database is not None:
                database.open()
                app.state.files = FileImportService(database, settings)
                app.state.imports = ImportsService(app.state.files)
                app.state.collections = CollectionsService(database)
                app.state.notes = NotesService(
                    database, processing_enabled=settings.processing_enabled
                )
                app.state.graph = GraphService(database)
                app.state.search = SearchService(database)
                app.state.chat = ChatService.from_env(database, app.state.notes, app.state.search)
            yield
        finally:
            if database is not None:
                database.close()

    app = FastAPI(title="Local Note Agent", version="0.2.0", lifespan=lifespan)

    def error(status, code, message):
        payload = ErrorResponse(error={"code": code, "message": message})
        return JSONResponse(status_code=status, content=payload.model_dump(mode="json"))

    @app.exception_handler(ChatError)
    async def chat_error(request: Request, exc: ChatError):
        return error(exc.status, exc.code, exc.message)

    @app.exception_handler(SearchError)
    async def search_error(request: Request, exc: SearchError):
        return error(503, exc.code, "Семантический поиск временно недоступен.")

    @app.exception_handler(NoteError)
    async def note_error(request: Request, exc: NoteError):
        status = {
            "note_not_found": 404,
            "import_not_found": 404,
            "collection_not_found": 404,
            "invalid_collection_title": 422,
            "attachment_not_found": 404,
            "note_not_ready": 409,
            "retry_not_available": 409,
            "database_unavailable": 503,
        }
        return error(status.get(exc.code, 500), exc.code, exc.message)

    @app.exception_handler(ProcessingError)
    async def import_error(request: Request, exc: ProcessingError):
        status = 413 if exc.code in {"file_too_large", "batch_too_large"} else 422
        return error(status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error(422, "validation_error", "Некорректные параметры запроса.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error(exc.status_code, "http_error", "Запрос не может быть выполнен.")

    @app.exception_handler(OperationalError)
    @app.exception_handler(PoolTimeout)
    async def database_error(request: Request, exc: Exception):
        return error(503, "database_unavailable", "База данных временно недоступна.")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        return error(500, "internal_error", "Внутренняя ошибка сервера.")

    app.include_router(file_router)
    app.include_router(import_router)
    app.include_router(collection_router)
    app.include_router(router)
    app.include_router(search_router)
    app.include_router(chat_router)
    app.include_router(graph_router)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.get("/api/v1/health", tags=["health"])
    def health() -> dict[str, str]:
        """API liveness; database and models have independent checks."""
        return {"status": "ok"}

    return app


app = create_app()
