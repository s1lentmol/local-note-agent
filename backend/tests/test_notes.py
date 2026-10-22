from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.models import Source


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "notes.sqlite3")) as client:
        yield client


def create_note(client, **changes):
    payload = {"source_type": "text", "title": "Конспект", "text": "Исходный текст"}
    response = client.post("/api/v1/notes", json=payload | changes)
    assert response.status_code == 201
    return response.json()


def test_health_and_openapi(client):
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    assert "/api/v1/notes" in client.get("/openapi.json").json()["paths"]


def test_note_lifecycle_preserves_original(client):
    original = "  Первая строка\r\nВторая строка  "
    note = create_note(client, text=original)
    path = f"/api/v1/notes/{note['id']}"
    assert note["content_version"] == 1
    assert note["ingestion_status"] == "ready"
    assert note["enrichment_status"] == "not_requested"
    assert note["created_at"].endswith("Z")
    assert client.get(path).json() == note

    edited = client.patch(path, json={
        "expected_version": 1, "title": "Исправлено", "extracted_text": "Новый текст",
    })
    assert edited.status_code == 200
    updated = edited.json()
    assert updated["title"] == "Исправлено"
    assert updated["extracted_text"] == "Новый текст"
    assert updated["source"]["original_text"] == original
    assert updated["content_version"] == 2
    assert updated["created_at"] == note["created_at"]
    assert updated["updated_at"] >= note["updated_at"]

    deleted = client.delete(path)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get(path).status_code == 404
    assert client.get("/api/v1/notes").json()["total"] == 0
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 0


def test_pagination_excludes_full_text(client):
    notes = [create_note(client, title=f"Заметка {i}") for i in range(3)]
    response = client.get("/api/v1/notes?limit=1&offset=1")
    page = response.json()
    assert response.status_code == 200
    assert (page["total"], page["limit"], page["offset"]) == (3, 1, 1)
    assert len(page["items"]) == 1
    assert page["items"][0]["id"] == notes[1]["id"]
    assert "extracted_text" not in page["items"][0]
    assert "source" not in page["items"][0]
    assert client.get("/api/v1/notes?offset=20").json()["items"] == []


def test_stale_update_is_rejected(client):
    note = create_note(client)
    path = f"/api/v1/notes/{note['id']}"
    assert client.patch(path, json={"expected_version": 1, "title": "Новое"}).status_code == 200
    stale = client.patch(path, json={"expected_version": 1, "extracted_text": "Устаревшее"})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"
    saved = client.get(path).json()
    assert saved["title"] == "Новое"
    assert saved["extracted_text"] == note["extracted_text"]


def test_concurrent_updates_do_not_overwrite_each_other(client):
    note = create_note(client)
    path = f"/api/v1/notes/{note['id']}"

    def edit(title):
        return client.patch(path, json={"expected_version": 1, "title": title})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(edit, ["Первое изменение", "Второе изменение"]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json() for response in responses if response.status_code == 200)
    assert client.get(path).json() == winner


def test_unchanged_update_keeps_version(client):
    note = create_note(client)
    response = client.patch(f"/api/v1/notes/{note['id']}", json={
        "expected_version": 1, "title": note["title"],
    })
    assert response.status_code == 200
    assert response.json() == note


@pytest.mark.parametrize("changes", [
    {"title": " "}, {"text": "\n\t"}, {"text": ""}, {"title": None},
    {"text": None}, {"title": "a" * 201}, {"text": "a" * 100_001},
    {"source_type": "url"}, {"unexpected": True},
])
def test_invalid_creation(client, changes):
    response = client.post("/api/v1/notes", json={
        "source_type": "text", "title": "Заметка", "text": "Текст",
    } | changes)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert client.get("/api/v1/notes").json()["total"] == 0


@pytest.mark.parametrize("payload", [
    {"expected_version": 1}, {"expected_version": 1, "title": None},
    {"expected_version": 1, "extracted_text": " "},
    {"expected_version": 0, "title": "Новое"}, {"title": "Новое"},
    {"expected_version": 1, "source": {"original_text": "Подмена"}},
])
def test_invalid_update(client, payload):
    note = create_note(client)
    path = f"/api/v1/notes/{note['id']}"
    response = client.patch(path, json=payload)
    assert response.status_code == 422
    assert client.get(path).json() == note


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
def test_invalid_pagination(client, query):
    assert client.get(f"/api/v1/notes?{query}").status_code == 422


def test_missing_and_invalid_ids(client):
    path = f"/api/v1/notes/{uuid4()}"
    for response in (
        client.get(path), client.delete(path),
        client.patch(path, json={"expected_version": 1, "title": "Новое"}),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "note_not_found"
    assert client.get("/api/v1/notes/not-a-uuid").status_code == 422


def test_notes_survive_restart(tmp_path):
    database_path = tmp_path / "nested" / "notes.sqlite3"
    with TestClient(create_app(database_path)) as first:
        note = create_note(first)
    with TestClient(create_app(database_path)) as restarted:
        assert restarted.get(f"/api/v1/notes/{note['id']}").json() == note
