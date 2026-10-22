import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import sessionmaker
from starlette.exceptions import HTTPException

from app.api.notes import router as notes_router
from app.db import Base, create_sqlite_engine


def create_app(database_path: Path | None = None) -> FastAPI:
    if database_path is None:
        data_dir = Path(os.environ.get(
            "NOTE_AGENT_DATA_DIR", Path(__file__).resolve().parents[1] / "data"
        ))
        database_path = data_dir / "notes.sqlite3"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_sqlite_engine(database_path)
        try:
            Base.metadata.create_all(engine)
            app.state.session_factory = sessionmaker(engine)
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="Local Note Agent", version="0.1.0", lifespan=lifespan)
    app.include_router(notes_router)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc: HTTPException):
        error = exc.detail if isinstance(exc.detail, dict) else {
            "code": "http_error", "message": str(exc.detail),
        }
        return JSONResponse({"error": error}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc: RequestValidationError):
        return JSONResponse({"error": {
            "code": "validation_error",
            "message": "Проверьте поля запроса и их допустимые значения.",
            "fields": [list(error["loc"]) for error in exc.errors()],
        }}, status_code=422)

    @app.get("/api/v1/health", tags=["health"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()
