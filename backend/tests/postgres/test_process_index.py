"""T08: real isolated PostgreSQL/pgvector, deterministic model adapters."""

import os
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from time import monotonic, sleep

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api_app import create_app
from app.config import Settings
from app.dto import NotePatch, TextNoteCreate
from app.indexing import ChunkSettings, ProcessHandler, build_index
from app.job_runner.worker import HandlerBinding, Worker
from app.jobs_repository import JobsRepository, StaleJob
from app.model_clients.settings import EmbeddingSettings
from app.postgres import Database
from app.repositories.index import IndexRepository, SpaceProfile
from app.services.notes import NotesService
from app.services.processing import enqueue_process, enqueue_ready
from tests.indexing_fixtures import FixtureEmbedder, failing_embedder, unit_vector


@pytest.fixture
def system(db, database_url):
    with Database(database_url) as database:
        yield NotesService(database, processing_enabled=True), JobsRepository(database)


def create(service, text="Mars colony plans", **kwargs):
    return service.create(TextNoteCreate(source_type="text", text=text, **kwargs))


def binding(database_url, factory=FixtureEmbedder):
    return HandlerBinding(
        ProcessHandler(database_url, EmbeddingSettings(), ChunkSettings(3, 1), factory),
        IndexRepository.publish,
    )


def result(text="Mars colony plans"):
    settings = EmbeddingSettings()
    return build_index(
        "title",
        text,
        FixtureEmbedder(settings),
        SpaceProfile.from_settings(settings),
        ChunkSettings(3, 1),
    )


def job_status(db):
    return db.execute(
        "SELECT status,error FROM jobs WHERE kind='process' ORDER BY created_at"
    ).fetchall()


def test_create_worker_vector_query_and_repeat(system, db, database_url):
    service, jobs = system
    mars = create(service)
    ocean = create(service, "Ocean waves and tides")
    assert mars.processing_status == "queued"
    worker = Worker(jobs, {"process": binding(database_url)})
    assert worker.run_once() and worker.run_once()
    assert service.get(mars.id).processing_status == "ready"
    assert service.get(ocean.id).processing_status == "ready"
    assert all(row == ("done", None) for row in job_status(db))
    found = db.execute(
        """SELECT note_id FROM chunks ORDER BY embedding <=> %s::vector LIMIT 1""",
        (str(unit_vector()),),
    ).fetchone()[0]
    assert found == mars.id
    before = db.execute("SELECT count(*) FROM chunks WHERE note_id=%s", (mars.id,)).fetchone()[0]
    with jobs.database.transaction() as conn:
        assert enqueue_process(conn, mars.id)
        assert not enqueue_process(conn, mars.id)
    assert worker.run_once()
    assert (
        db.execute("SELECT count(*) FROM chunks WHERE note_id=%s", (mars.id,)).fetchone()[0]
        == before
    )
    assert (
        db.execute(
            "SELECT count(DISTINCT job_id) FROM chunks WHERE note_id=%s", (mars.id,)
        ).fetchone()[0]
        == 1
    )


def test_create_and_job_rollback(system, db, monkeypatch):
    service, _ = system
    monkeypatch.setattr(service, "_detail", lambda *args: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        create(service)
    for table in ("notes", "sources", "jobs"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_model_error_safe_states_and_retry_api(system, db, database_url):
    service, jobs = system
    note = create(service)
    worker = Worker(jobs, {"process": binding(database_url, failing_embedder)})
    worker.run_once()
    failed = service.get(note.id)
    assert failed.processing_status == "failed"
    assert failed.processing_error.code == "handler_failed"
    assert failed.extracted_text == note.extracted_text
    assert "synthetic_model_failure" not in str(job_status(db))
    assert enqueue_ready(jobs.database) == 0
    with TestClient(create_app(Settings(database_url=database_url))) as client:
        path = f"/api/v1/notes/{note.id}/retry"
        assert client.post(path, json={"stage": "extract"}).status_code == 409
        response = client.post(path, json={"stage": "process"})
        assert response.status_code == 202
        assert response.json()["processing_status"] == "queued"
        assert response.json() == client.get(f"/api/v1/notes/{note.id}").json()
        assert client.post(path, json={"stage": "process"}).status_code == 409
        assert client.post(path, json={"stage": "other"}).status_code == 422
    assert Worker(jobs, {"process": binding(database_url)}).run_once()
    assert service.get(note.id).processing_status == "ready"
    assert db.execute("SELECT attempts FROM jobs").fetchone()[0] == 2
    service.delete(note.id)
    with TestClient(create_app(Settings(database_url=database_url))) as client:
        assert client.post(path, json={"stage": "process"}).status_code == 404


@pytest.mark.parametrize(
    "column,value",
    [
        ("model", "wrong"),
        ("revision", "0" * 64),
        ("normalization", "l2"),
        ("query_instruction", "wrong"),
    ],
)
def test_profile_checked_before_compute_and_at_publication(system, db, database_url, column, value):
    service, jobs = system
    note = create(service)
    # normalization has a schema CHECK; this case tests a mismatched payload instead.
    if column == "normalization":
        job = jobs.claim(10)
        payload = result()
        payload = replace(payload, profile=replace(payload.profile, normalization="none"))
        with pytest.raises(ValueError):
            jobs.publish(job, IndexRepository.publish, payload)
    else:
        db.execute(f"UPDATE embedding_profiles SET {column}=%s", (value,))
        Worker(jobs, {"process": binding(database_url)}).run_once()
        assert service.get(note.id).processing_status == "failed"
        assert jobs.retry(db.execute("SELECT id FROM jobs").fetchone()[0])
        job = jobs.claim(10)
        with pytest.raises(ValueError):
            jobs.publish(job, IndexRepository.publish, result())
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


def test_atomic_replace_rollback_and_profile_guard(system, db):
    service, jobs = system
    note = create(service)
    job = jobs.claim(10)
    jobs.publish(job, IndexRepository.publish, result())
    previous = db.execute("SELECT id,text FROM chunks ORDER BY position").fetchall()
    with pytest.raises(psycopg.errors.RaiseException):
        db.execute("UPDATE embedding_profiles SET model='other'")
    broken = replace(result(), vectors=[unit_vector(), [1.0]])
    with pytest.raises(psycopg.Error):
        jobs.publish(job, IndexRepository.publish, broken)
    assert db.execute("SELECT id,text FROM chunks ORDER BY position").fetchall() == previous
    jobs.publish(job, IndexRepository.publish, result(), complete=True)
    assert service.get(note.id).processing_status == "ready"


@pytest.mark.parametrize("mutation", ["patch", "delete", "expire", "token"])
def test_stale_index_rejected(system, db, mutation):
    service, jobs = system
    note = create(service)
    job = jobs.claim(10)
    if mutation == "patch":
        service.patch(note.id, NotePatch(extracted_text="new content"))
    elif mutation == "delete":
        service.delete(note.id)
    elif mutation == "expire":
        db.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second'")
    else:
        db.execute("UPDATE jobs SET lease_token=gen_random_uuid()")
    with pytest.raises(StaleJob):
        jobs.publish(job, IndexRepository.publish, result(), complete=True)
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


def test_lease_expires_waiting_profile_lock(system, db, database_url):
    service, jobs = system
    create(service)
    job = jobs.claim(0.3)
    with psycopg.connect(database_url) as blocker, ThreadPoolExecutor(1) as executor:
        blocker.execute("SELECT id FROM embedding_profiles FOR UPDATE")
        future = executor.submit(jobs.publish, job, IndexRepository.publish, result())
        sleep(0.4)
        blocker.commit()
        with pytest.raises(StaleJob):
            future.result(timeout=3)
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    assert jobs.recover_expired() == 1
    assert service.get(job.note_id).processing_status == "queued"


def test_backlog_concurrent_excludes_deleted_and_not_ready(system, db):
    service, jobs = system
    disabled = NotesService(jobs.database)
    ready = [create(disabled, f"text {i}") for i in range(4)]
    deleted = create(disabled)
    service.delete(deleted.id)
    not_ready = create(disabled)
    db.execute("UPDATE notes SET ingestion_status='queued' WHERE id=%s", (not_ready.id,))
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda _: enqueue_ready(jobs.database), range(2)))
    assert sum(results) == len(ready)
    assert enqueue_ready(jobs.database) == 0
    assert db.execute("SELECT count(*) FROM jobs WHERE kind='process'").fetchone()[0] == len(ready)
    assert service.get(not_ready.id).processing_status == "not_requested"


def test_api_enabled_and_disabled(db, database_url):
    for enabled, status in [(False, "not_requested"), (True, "queued")]:
        with TestClient(
            create_app(Settings(database_url=database_url, processing_enabled=enabled))
        ) as client:
            response = client.post("/api/v1/notes", json={"source_type": "text", "text": "Mars"})
            assert response.status_code == 201
            assert response.json()["processing_status"] == status
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_default_worker_cli_with_http_endpoint(system, db, database_url):
    """Production factory + T07 HTTP protocol; local deterministic service, no weights."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    service, _ = system
    note = create(service)
    requests = []
    settings = EmbeddingSettings()

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self):
            assert self.path == "/api/tags"
            self.respond({"models": [{"name": settings.model, "digest": settings.digest}]})

        def do_POST(self):
            assert self.path == "/api/embed"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            self.respond(
                {"model": settings.model, "embeddings": [unit_vector() for _ in body["input"]]}
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    env = dict(
        os.environ,
        NOTE_AGENT_DATABASE_URL=database_url,
        EMBEDDING_BASE_URL=f"http://127.0.0.1:{server.server_port}",
        NOTE_AGENT_PG_WORKER_POLL_SECONDS="0.05",
    )
    env.pop("NOTE_AGENT_PG_WORKER_HANDLERS", None)
    process = subprocess.Popen([sys.executable, "-m", "app.postgres_worker"], env=env)
    try:
        deadline = monotonic() + 10
        while job_status(db)[0][0] not in ("done", "failed"):
            assert process.poll() is None
            assert monotonic() < deadline
            sleep(0.02)
        assert job_status(db) == [("done", None)]
        assert service.get(note.id).processing_status == "ready"
        assert requests[0]["input"] == [f"{note.title} {note.extracted_text}"]
        assert requests[0]["truncate"] is False
        assert db.execute("SELECT note_id FROM chunks").fetchone()[0] == note.id
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(3)
