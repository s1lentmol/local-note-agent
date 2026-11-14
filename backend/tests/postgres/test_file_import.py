"""T11 with isolated PostgreSQL/pgvector, temporary originals and fake models."""

from dataclasses import replace
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from app.api_app import create_app
from app.config import Settings
from app.dto import FileImportFields, NotePatch
from app.errors import ProcessingError
from app.extraction import ExtractHandler, ExtractResult, publish_extract
from app.indexing import handlers
from app.job_runner.worker import HandlerBinding, Worker
from app.jobs_repository import JobsRepository, StaleJob
from app.postgres import Database
from app.repositories.sources import SourcesRepository
from app.services.file_import import FileImportService
from app.services.notes import NoteError, NotesService
from app.services.storage import Storage
from tests.enrichment_fixtures import GeneratorFactory
from tests.indexing_fixtures import FixtureEmbedder


@pytest.fixture
def system(db, database_url, tmp_path, monkeypatch):
    monkeypatch.setenv("NOTE_AGENT_DATABASE_URL", database_url)
    monkeypatch.setenv("NOTE_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTE_AGENT_PROCESSING_ENABLED", "true")
    monkeypatch.setenv("NOTE_AGENT_ENRICHMENT_ENABLED", "true")
    settings = Settings(database_url=database_url, data_dir=tmp_path, processing_enabled=True)
    with Database(database_url) as database:
        yield (
            FileImportService(database, settings),
            NotesService(database, processing_enabled=True),
            JobsRepository(database),
            settings,
        )


def upload(files, data=b"# Mars\n\nBody\n", name="../Mars.md", title=None):
    return files.import_file(
        BytesIO(data), name, FileImportFields(source_type="text_file", title=title)
    )


def worker(jobs):
    bindings = handlers()
    bindings["process"] = HandlerBinding(
        replace(
            bindings["process"].compute,
            embedder_factory=FixtureEmbedder,
            generator_factory=GeneratorFactory(),
        ),
        bindings["process"].writer,
    )
    return Worker(jobs, bindings)


def test_upload_extract_process_download_cleanup(system, db):
    files, notes, jobs, settings = system
    data = b"\xef\xbb\xbf# Mars\r\n\r\n  original body\n"
    note = upload(files, data)
    assert (note.title, note.extracted_text, note.ingestion_status) == ("Mars.md", None, "queued")
    assert db.execute("SELECT title_is_explicit FROM notes").fetchone() == (False,)
    key = db.execute("SELECT storage_key FROM attachments").fetchone()[0]
    assert "Mars" not in key and ".." not in key
    with TestClient(create_app(settings)) as client:
        url = note.attachments[0].download_url
        response = client.get(url)
        assert response.status_code == 200 and response.content == data
        assert "storage_key" not in client.get(f"/api/v1/notes/{note.id}").text
        assert worker(jobs).run_once()
        ready = notes.get(note.id)
        assert ready.extracted_text == data.decode("utf-8-sig")
        assert (ready.ingestion_status, ready.processing_status) == ("ready", "queued")
        assert worker(jobs).run_once()
        assert notes.get(note.id).processing_status == "ready"
        assert notes.get(note.id).summary == "Plans for Mars"
        assert db.execute("SELECT status FROM jobs WHERE kind='extract'").fetchone() == ("done",)
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] > 0
        notes.delete(note.id)
        assert client.get(url).status_code == 404
        assert worker(jobs).run_once()
        assert not Storage(settings.data_dir).path(key).exists()
        assert db.execute("SELECT count(*) FROM attachments").fetchone() == (0,)


@pytest.mark.parametrize("title", [None, "Явный заголовок"])
def test_multipart_title_and_processing_disabled(system, db, title):
    _, _, jobs, settings = system
    settings = settings.model_copy(update={"processing_enabled": False})
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/notes/import",
            data={"source_type": "text_file", **({"title": title} if title else {})},
            files={"file": ("Конспект.txt", "Абзац\n\nВторой".encode(), "image/png")},
        )
        assert response.status_code == 202, response.text
        assert response.json()["title"] == (title or "Конспект.txt")
        assert db.execute("SELECT title_is_explicit FROM notes").fetchone() == (title is not None,)
        bindings = handlers()
        bindings["extract"] = HandlerBinding(
            ExtractHandler(settings.database_url, settings),
            publish_extract,
            complete_on_publish=True,
        )
        assert Worker(jobs, bindings).run_once()
        detail = client.get("/api/v1/notes/" + response.json()["id"]).json()
        assert detail["ingestion_status"] == "ready"
        assert detail["processing_status"] == "not_requested"
        assert db.execute("SELECT count(*) FROM jobs").fetchone() == (1,)


@pytest.mark.parametrize(
    "data,code",
    [
        (b"\xff", "invalid_encoding"),
        (b"\x00", "unsupported_text"),
        (b" \n", "empty_text"),
        (b"a" * 100001, "text_too_large"),
    ],
)
def test_failed_extract_retry_same_job(system, db, data, code):
    files, notes, jobs, settings = system
    note = upload(files, data)
    assert worker(jobs).run_once()
    failed = notes.get(note.id)
    assert failed.ingestion_status == "failed" and failed.ingestion_error.code == code
    job_id = db.execute("SELECT id FROM jobs").fetchone()[0]
    with TestClient(create_app(settings)) as client:
        url = f"/api/v1/notes/{note.id}/retry"
        assert client.post(url, json={"stage": "extract"}).status_code == 202
        assert client.post(url, json={"stage": "extract"}).status_code == 409
    assert db.execute("SELECT id,status FROM jobs").fetchall() == [(job_id, "queued")]
    assert worker(jobs).run_once()
    assert db.execute("SELECT id,attempts FROM jobs").fetchall() == [(job_id, 2)]


@pytest.mark.parametrize(
    "data,code", [(b"", "empty_file"), (b"a" * (1048576 + 1), "file_too_large")]
)
def test_upload_limits_leave_no_file(system, db, data, code):
    files, _, _, settings = system
    with pytest.raises(ProcessingError) as exc:
        upload(files, data)
    assert exc.value.code == code
    assert not [p for p in (settings.data_dir / "uploads").rglob("*") if p.is_file()]
    assert db.execute("SELECT count(*) FROM notes").fetchone() == (0,)


def test_rollback_and_write_failure(system, db, monkeypatch):
    files, _, _, settings = system

    def fail(*args):
        raise RuntimeError("synthetic DB failure")

    monkeypatch.setattr(SourcesRepository, "create_file", fail)
    with pytest.raises(RuntimeError):
        upload(files)
    assert db.execute("SELECT count(*) FROM notes").fetchone() == (0,)
    assert not [p for p in (settings.data_dir / "uploads").rglob("*") if p.is_file()]

    class BrokenStream:
        count = 0

        def read(self, size):
            self.count += 1
            if self.count == 1:
                return b"part"
            raise OSError("synthetic IO failure")

    with pytest.raises(OSError):
        files.import_file(BrokenStream(), "x.txt", FileImportFields(source_type="text_file"))
    assert not [p for p in (settings.data_dir / "uploads").rglob("*") if p.is_file()]


def test_io_and_decode_outside_transactions(system, db, monkeypatch):
    files, notes, jobs, settings = system
    import app.extraction as extraction

    original_save = Storage.save
    original_decode = extraction.extract_text

    def check_no_transaction():
        assert (
            db.execute(
                """SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
               AND pid<>pg_backend_pid() AND state='idle in transaction'"""
            ).fetchone()[0]
            == 0
        )

    def save(*args, **kwargs):
        check_no_transaction()
        return original_save(*args, **kwargs)

    def decode(*args):
        check_no_transaction()
        return original_decode(*args)

    monkeypatch.setattr(Storage, "save", save)
    monkeypatch.setattr(extraction, "extract_text", decode)
    note = upload(files)
    occupation = jobs.claim(30)

    class Context:
        def publish(self, result):
            jobs.publish(occupation, publish_extract, result)

    ExtractHandler(settings.database_url, settings)(occupation, Context())
    jobs.complete(occupation)
    assert notes.get(note.id).ingestion_status == "ready"


def test_patch_delete_and_expired_extract_cannot_publish(system, db):
    files, notes, jobs, _ = system
    note = upload(files)
    old = jobs.claim(30)
    with pytest.raises(NoteError, match="не завершено"):
        notes.patch(note.id, NotePatch(extracted_text="edit"))
    assert notes.get(note.id).extracted_text is None
    db.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second'")
    with pytest.raises(StaleJob):
        jobs.publish(old, publish_extract, ExtractResult("old", True))
    assert jobs.recover_expired() == 1
    current = jobs.claim(30)
    with pytest.raises(StaleJob):
        jobs.publish(old, publish_extract, ExtractResult("old", True))
    # PATCH is allowed after publication and cancels the still-running extract.
    jobs.publish(current, publish_extract, ExtractResult("first", True))
    notes.patch(note.id, NotePatch(extracted_text="edit"))
    with pytest.raises(StaleJob):
        jobs.publish(current, publish_extract, ExtractResult("old", True))
    assert notes.get(note.id).extracted_text == "edit"
    other = upload(files)
    # The previous note has a queued process: occupy it first.
    process = jobs.claim(30)
    jobs.complete(process)
    extracting = jobs.claim(30)
    assert extracting.note_id == other.id
    notes.delete(other.id)
    with pytest.raises(StaleJob):
        jobs.publish(extracting, publish_extract, ExtractResult("old", True))
    assert db.execute(
        "SELECT count(*) FROM jobs WHERE note_id=%s AND kind='process'", (other.id,)
    ).fetchone() == (0,)


def test_missing_original_and_unsupported_format(system, db):
    files, _, jobs, settings = system
    with pytest.raises(ProcessingError):
        upload(files, name="a.pdf")
    note = upload(files)
    key = db.execute("SELECT storage_key FROM attachments").fetchone()[0]
    Storage(settings.data_dir).remove(key)
    with TestClient(create_app(settings)) as client:
        assert client.get(note.attachments[0].download_url).status_code == 404
        assert (
            client.post(
                "/api/v1/notes/import", data={"source_type": "pdf"}, files={"file": ("x.pdf", b"x")}
            ).status_code
            == 422
        )
    assert worker(jobs).run_once()
    assert db.execute("SELECT ingestion_error->>'code' FROM notes").fetchone() == (
        "original_missing",
    )


def test_multipart_rejects_multiple_files_and_size(system, db):
    _, _, _, settings = system
    with TestClient(create_app(settings)) as client:
        for uploads in (
            [("file", ("a.txt", b"a")), ("file", ("b.txt", b"b"))],
            [("file", ("a.txt", b"a")), ("other", ("b.txt", b"b"))],
        ):
            assert (
                client.post(
                    "/api/v1/notes/import", data={"source_type": "text_file"}, files=uploads
                ).status_code
                == 422
            )
        response = client.post(
            "/api/v1/notes/import",
            data={"source_type": "text_file"},
            files={"file": ("big.txt", b"a" * (1048576 + 1))},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "file_too_large"
    assert db.execute("SELECT count(*) FROM notes").fetchone() == (0,)


def test_publication_rollback_includes_process_and_job(system, db, monkeypatch):
    files, notes, jobs, _ = system
    import app.extraction as extraction
    from app.services.processing import enqueue_process

    note = upload(files)
    occupation = jobs.claim(30)

    def fail_after_enqueue(conn, note_id):
        enqueue_process(conn, note_id)
        raise RuntimeError("synthetic publication failure")

    monkeypatch.setattr(extraction, "enqueue_process", fail_after_enqueue)
    with pytest.raises(RuntimeError):
        jobs.publish(occupation, publish_extract, ExtractResult("body", True), complete=True)
    detail = notes.get(note.id)
    assert detail.extracted_text is None and detail.ingestion_status == "processing"
    assert detail.processing_status == "not_requested"
    assert db.execute("SELECT kind,status FROM jobs").fetchall() == [("extract", "processing")]


def test_exact_upload_limit_preserves_original(system):
    files, _, _, _ = system
    data = b"a" * 1048576
    note = upload(files, data)
    stream, _ = files.download(note.attachments[0].id)
    with stream:
        assert stream.read() == data
