"""API entrypoint for the PostgreSQL deployment."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException

from app.api.note_crud import router
from app.api.search import router as search_router
from app.config import Settings
from app.dto import ErrorResponse
from app.postgres import Database
from app.services.notes import NoteError, NotesService
from app.services.search import SearchError, SearchService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        database = Database(settings.database_url) if settings.database_url else None
        app.state.notes = None
        app.state.search = None
        try:
            if database is not None:
                database.open()
                app.state.notes = NotesService(
                    database, processing_enabled=settings.processing_enabled
                )
                app.state.search = SearchService(database)
            yield
        finally:
            if database is not None:
                database.close()

    app = FastAPI(title="Local Note Agent", version="0.2.0", lifespan=lifespan)

    def error(status, code, message):
        payload = ErrorResponse(error={"code": code, "message": message})
        return JSONResponse(status_code=status, content=payload.model_dump(mode="json"))

    @app.exception_handler(SearchError)
    async def search_error(request: Request, exc: SearchError):
        return error(503, exc.code, "Семантический поиск временно недоступен.")

    @app.exception_handler(NoteError)
    async def note_error(request: Request, exc: NoteError):
        status = {
            "note_not_found": 404,
            "note_not_ready": 409,
            "retry_not_available": 409,
            "database_unavailable": 503,
        }
        return error(status.get(exc.code, 500), exc.code, exc.message)

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

    app.include_router(router)
    app.include_router(search_router)
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
