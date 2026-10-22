import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

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

    @app.get("/api/v1/health", tags=["health"])
    def health():
        return {"status": "ok"}

    return app


app = create_app()
