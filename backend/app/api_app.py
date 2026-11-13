"""API entrypoint for the PostgreSQL deployment."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Local Note Agent", version="0.2.0")
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET"],
        allow_headers=["Content-Type"],
    )

    @app.get("/api/v1/health", tags=["health"])
    def health() -> dict[str, str]:
        """API liveness; database and models have independent checks."""
        return {"status": "ok"}

    return app


app = create_app()
