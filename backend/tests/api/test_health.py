import os
import subprocess
import sys

from fastapi.testclient import TestClient

from app.api_app import create_app
from app.config import Settings


class FakeDatabase:
    def __init__(self, url):
        self.opened = False

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False


def test_health_without_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTE_AGENT_DATA_DIR", str(tmp_path / "unused"))
    monkeypatch.setenv("NOTE_AGENT_DATABASE_URL", "postgresql://unreachable.invalid/test")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr("app.api_app.Database", FakeDatabase)
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
            "NOTE_AGENT_DATABASE_URL": "",
        },
    )
    assert list(tmp_path.iterdir()) == []


def test_pool_lifecycle(monkeypatch):
    instances = []

    class TrackedDatabase(FakeDatabase):
        def __init__(self, url):
            super().__init__(url)
            instances.append(self)

    monkeypatch.setattr("app.api_app.Database", TrackedDatabase)
    app = create_app(Settings(database_url="postgresql://unused/test"))
    assert not instances
    with TestClient(app):
        assert instances[0].opened
        assert app.state.notes.database is instances[0]
    assert not instances[0].opened


def test_unconfigured_api_errors():
    with TestClient(create_app(Settings(database_url=""))) as client:
        assert client.get("/api/v1/health").status_code == 200
        response = client.get("/api/v1/notes")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "database_unavailable"
        assert client.get("/api/v1/missing").status_code == 404
