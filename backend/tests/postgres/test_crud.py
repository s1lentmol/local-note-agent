from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api_app import create_app
from app.config import Settings
from app.dto import NoteListQuery, NotePatch, TextNoteCreate
from app.postgres import Database
from app.repositories.sources import SourcesRepository
from app.services.notes import NotesService


@pytest.fixture
def service(db, database_url):
    with Database(database_url) as database:
        yield NotesService(database)


@pytest.fixture
def client(db, database_url):
    with TestClient(create_app(Settings(database_url=database_url))) as client:
        yield client


def create(service, text="Первый заголовок\nСодержимое", **kwargs):
    return service.create(TextNoteCreate(source_type="text", text=text, **kwargs))


def test_persistence_original_and_patch(service, db, database_url):
    text = "  Оригинальный заголовок\n\n  текст  \n"
    note = create(service, text)
    assert note.title == "Оригинальный заголовок"
    assert note.extracted_text == note.source.original_text == text
    assert note.ingestion_status == "ready"
    assert note.processing_status == "not_requested"
    assert note.summary is None and note.tags == note.entities == []
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    with Database(database_url) as reopened:
        assert NotesService(reopened).get(note.id) == note
    updated = service.patch(note.id, NotePatch(title="Новый", extracted_text="  новый текст\n"))
    assert updated.title == "Новый"
    assert updated.extracted_text == "  новый текст\n"
    assert updated.source.original_text == text
    assert updated.created_at == note.created_at and updated.updated_at > note.updated_at
    assert db.execute("SELECT title_is_explicit FROM notes WHERE id=%s", (note.id,)).fetchone()[0]


def test_long_default_and_explicit_title(service):
    assert create(service, "x" * 250).title == "x" * 200
    assert create(service, title="Явный").title == "Явный"


def seed_job(db, note_id, kind="process", status="queued"):
    return db.execute(
        """INSERT INTO jobs(note_id, kind, status, lease_token, lease_until, started_at)
           VALUES (%s, %s, %s, gen_random_uuid(), now()+interval '1 minute', now()) RETURNING id""",
        (note_id, kind, status),
    ).fetchone()[0]


def test_patch_cancels_jobs_and_clears_derived_preserving_manual_data(service, db):
    note, other = create(service), create(service)
    job = seed_job(db, note.id, status="processing")
    seed_job(db, note.id, "extract")
    db.execute(
        """UPDATE notes SET summary='old', tags='["auto"]', manual_tags='["manual"]',
           entities='["entity"]', category='category', processing_status='processing',
           processing_error='{"code":"old","message":"old"}' WHERE id=%s""",
        (note.id,),
    )
    db.execute(
        """INSERT INTO chunks(note_id,job_id,position,text,embedding)
           VALUES (%s,%s,0,'old',%s::vector)""",
        (note.id, job, str([1.0] * 1024)),
    )
    db.execute("INSERT INTO enrichments VALUES (%s,%s,'fake','v1',now())", (note.id, job))
    a, b = sorted([note.id, other.id])
    db.execute("INSERT INTO note_links VALUES (%s,%s,'semantic',0.5,'test',now())", (a, b))
    collection = db.execute(
        "INSERT INTO collections(title) VALUES ('manual') RETURNING id"
    ).fetchone()[0]
    db.execute(
        "INSERT INTO collection_notes(collection_id,note_id) VALUES (%s,%s)", (collection, note.id)
    )
    changed = service.patch(note.id, NotePatch(title="title only"))
    assert changed.extracted_text == note.extracted_text
    assert changed.processing_status == "not_requested"
    assert changed.summary is changed.category is changed.processing_error is None
    assert changed.tags == changed.entities == []
    assert db.execute("SELECT manual_tags FROM notes WHERE id=%s", (note.id,)).fetchone()[0] == [
        "manual"
    ]
    assert db.execute("SELECT count(*) FROM collection_notes").fetchone()[0] == 1
    for table in ("chunks", "enrichments", "note_links"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    jobs = db.execute("SELECT status, finished_at, lease_token, lease_until FROM jobs").fetchall()
    assert all(row[0] == "cancelled" and row[1] and row[2:] == (None, None) for row in jobs)


def test_create_rollback(service, db, monkeypatch):
    def fail(*args):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(SourcesRepository, "create_text", fail)
    with pytest.raises(RuntimeError):
        create(service)
    assert db.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["patch", "delete"])
def test_mutation_rollback(service, db, monkeypatch, operation):
    note = create(service)
    job = seed_job(db, note.id)
    if operation == "patch":

        def fail(*args):
            raise RuntimeError("injected after write")

        monkeypatch.setattr(service, "_detail", fail)

        def call():
            return service.patch(note.id, NotePatch(extracted_text="changed"))
    else:
        from app.repositories.notes import NotesRepository

        def fail(*args):
            raise RuntimeError("injected after delete")

        monkeypatch.setattr(NotesRepository, "queue_cleanup", fail)

        def call():
            return service.delete(note.id)

    with pytest.raises(RuntimeError):
        call()
    row = db.execute(
        "SELECT extracted_text,deleted_at FROM notes WHERE id=%s", (note.id,)
    ).fetchone()
    assert row == (note.extracted_text, None)
    assert db.execute("SELECT status FROM jobs WHERE id=%s", (job,)).fetchone()[0] == "queued"


def test_delete_hides_and_retries_cleanup(client, db):
    note = client.post("/api/v1/notes", json={"source_type": "text", "text": "original"}).json()
    note_id = note["id"]
    seed_job(db, note_id, status="processing")
    path = "/api/v1/notes/" + note_id
    assert client.delete(path).status_code == 204
    cleanup = db.execute("SELECT id FROM jobs WHERE kind='cleanup'").fetchone()[0]
    assert client.delete(path).status_code == 204
    assert db.execute("SELECT count(*) FROM jobs WHERE kind='cleanup'").fetchone()[0] == 1
    for response in (client.get(path), client.patch(path, json={"title": "x"})):
        assert response.status_code == 404
    assert client.get("/api/v1/notes").json()["total"] == 0
    assert db.execute("SELECT original_text FROM sources").fetchone()[0] == "original"
    db.execute("UPDATE jobs SET status='failed',finished_at=now() WHERE id=%s", (cleanup,))
    assert client.delete(path).status_code == 204
    assert (
        db.execute("SELECT count(*) FROM jobs WHERE kind='cleanup' AND status='queued'").fetchone()[
            0
        ]
        == 1
    )
    assert client.delete("/api/v1/notes/" + str(uuid4())).status_code == 404


def test_list_filters_pagination_stable_order(service, db, client):
    notes = [create(service, f"text {i}") for i in range(5)]
    db.execute("UPDATE notes SET created_at='2026-01-01T00:00:00Z'")
    db.execute("UPDATE notes SET processing_status='failed' WHERE id=%s", (notes[0].id,))
    db.execute(
        "UPDATE sources SET type='text_file', original_text=NULL WHERE note_id=%s", (notes[1].id,)
    )
    service.delete(notes[4].id)
    ordered = sorted([n.id for n in notes[:4]], reverse=True)
    ids = []
    for offset in (0, 2):
        page = client.get("/api/v1/notes", params={"limit": 2, "offset": offset}).json()
        assert page["total"] == 4
        assert page["limit"] == 2 and page["offset"] == offset
        assert all("extracted_text" not in item and "source" not in item for item in page["items"])
        ids.extend(item["id"] for item in page["items"])
    assert ids == [str(x) for x in ordered]
    assert service.list(NoteListQuery(offset=100)).total == 4
    assert service.list(NoteListQuery(source_type="text_file")).total == 1
    assert (
        service.list(
            NoteListQuery(source_type="text", ingestion_status="ready", processing_status="failed")
        ).total
        == 1
    )
    assert service.list(NoteListQuery(ingestion_status="failed")).total == 0


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"source_type": "text", "text": ""},
        {"source_type": "text", "text": "  \n"},
        {"source_type": "text", "text": "x", "extra": 1},
        {"source_type": "url", "url": "https://example.org"},
        {"source_type": "text", "text": "x", "title": ""},
        {"source_type": "text", "text": "x" * 100001},
        {"source_type": "text", "text": "a\x00b"},
        {"source_type": "text", "text": "x", "title": "a\x00b"},
    ],
)
def test_invalid_creation(client, db, body):
    response = client.post("/api/v1/notes", json=body)
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "validation_error", "message": "Некорректные параметры запроса."}
    }
    assert db.execute("SELECT count(*) FROM notes").fetchone()[0] == 0


def test_api_validation_and_conflict(client, db):
    response = client.post("/api/v1/notes", json={"source_type": "text", "text": "body"})
    assert response.status_code == 201
    note = response.json()
    path = "/api/v1/notes/" + note["id"]
    assert client.get(path).json() == note
    for body in (
        {},
        {"title": None},
        {"extracted_text": "  "},
        {"expected_version": 1},
        {"title": "x" * 201},
    ):
        assert client.patch(path, json=body).status_code == 422
    assert (
        client.patch(path, json={"title": "new", "extracted_text": "new body"}).json()["source"][
            "original_text"
        ]
        == "body"
    )
    assert client.get("/api/v1/notes/not-a-uuid").status_code == 422
    assert client.get("/api/v1/notes/" + str(uuid4())).status_code == 404
    for params in (
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"source_type": "bad"},
        {"processing_status": "bad"},
        {"unknown": "x"},
    ):
        assert client.get("/api/v1/notes", params=params).status_code == 422
    assert (
        client.post(
            "/api/v1/notes", content="{", headers={"Content-Type": "application/json"}
        ).status_code
        == 422
    )
    db.execute(
        "UPDATE notes SET ingestion_status='processing', extracted_text=NULL WHERE id=%s",
        (note["id"],),
    )
    assert client.patch(path, json={"title": "x"}).status_code == 409
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("operation", ["patch", "delete"])
def test_mutation_locks_note_before_job(service, db, database_url, operation):
    note = create(service)
    job = seed_job(db, note.id)
    with psycopg.connect(database_url) as blocker, ThreadPoolExecutor(max_workers=1) as executor:
        blocker.execute("SELECT id FROM jobs WHERE id=%s FOR UPDATE", (job,))
        if operation == "patch":
            future = executor.submit(service.patch, note.id, NotePatch(title="after lock"))
        else:
            future = executor.submit(service.delete, note.id)
        try:
            deadline = monotonic() + 5
            while True:
                with psycopg.connect(database_url, autocommit=True) as observer:
                    try:
                        with observer.transaction():
                            observer.execute(
                                "SELECT id FROM notes WHERE id=%s FOR UPDATE NOWAIT", (note.id,)
                            )
                    except psycopg.errors.LockNotAvailable:
                        break
                if monotonic() >= deadline:
                    pytest.fail("Mutation did not lock note while waiting for job")
                sleep(0.01)
            assert not future.done()
        finally:
            blocker.commit()
        result = future.result(timeout=5)
        if operation == "patch":
            assert result.title == "after lock"
        else:
            assert result is None
    assert db.execute("SELECT status FROM jobs WHERE id=%s", (job,)).fetchone()[0] == "cancelled"
