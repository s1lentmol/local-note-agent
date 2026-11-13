import os
import subprocess
import sys

from fastapi.testclient import TestClient

from app.api_app import create_app
from app.config import Settings


def test_health_without_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTE_AGENT_DATA_DIR", str(tmp_path / "unused"))
    monkeypatch.setenv("NOTE_AGENT_DATABASE_URL", "postgresql://unreachable.invalid/test")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/health").json() == {"status": "ok"}
    assert list(tmp_path.iterdir()) == []


def test_configuration(monkeypatch):
    monkeypatch.setenv("NOTE_AGENT_DATABASE_URL", "postgresql://user:secret@db/notes")
    settings = Settings.from_env()
    assert settings.database_url == "postgresql://user:secret@db/notes"
    assert "secret" not in repr(settings)


def test_startup_does_not_open_sqlite(tmp_path):
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sqlite3

def reject_sqlite(*args, **kwargs):
    raise AssertionError("API must not open SQLite")

sqlite3.connect = reject_sqlite
from fastapi.testclient import TestClient
from app.api_app import create_app

with TestClient(create_app()) as client:
    assert client.get("/api/v1/health").status_code == 200
""",
        ],
        check=True,
        env={
            **{key: value for key, value in os.environ.items() if key != "TELEGRAM_BOT_TOKEN"},
            "PYTHONDONTWRITEBYTECODE": "1",
            "NOTE_AGENT_DATA_DIR": str(tmp_path / "unused"),
        },
    )
    assert list(tmp_path.iterdir()) == []
