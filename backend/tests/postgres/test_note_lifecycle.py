"""T14 lifecycle tests against PostgreSQL/pgvector and disposable uploads."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.dto import Enrichment, NoteListQuery, NotePatch, SearchRequest, TextNoteCreate
from app.enrichment import EnrichmentResult
from app.indexing import handlers, publish_process
from app.job_runner.worker import Worker
from app.jobs_repository import JobsRepository, StaleJob
from app.model_clients.settings import EmbeddingSettings
from app.postgres import Database
from app.repositories.index import IndexResult, SpaceProfile
from app.repositories.sources import TelegramDelivery
from app.services.notes import NoteError, NotesService
from app.services.search import SearchService
from app.services.storage import Storage
from tests.indexing_fixtures import FixtureEmbedder, unit_vector


@pytest.fixture
def system(db, database_url, tmp_path, monkeypatch):
    monkeypatch.setenv("NOTE_AGENT_DATABASE_URL", database_url)
    monkeypatch.setenv("NOTE_AGENT_DATA_DIR", str(tmp_path))
    with Database(database_url) as database:
        yield NotesService(database, processing_enabled=True), JobsRepository(database)


def create(service, text="Mars original", **kwargs):
    return service.create(TextNoteCreate(source_type="text", text=text), **kwargs)


def add_files(db, note, storage, names):
    source_id = db.execute("SELECT id FROM sources WHERE note_id=%s", (note.id,)).fetchone()[0]
    keys = []
    for name in names:
        key = f"{source_id}/{name}"
        path = storage.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original")
        keys.append(key)
        db.execute(
            """INSERT INTO attachments(source_id, storage_key, original_name, mime_type, size_bytes)
               VALUES (%s,%s,%s,'text/plain',8)""",
            (source_id, key, name),
        )
    return keys


def publish_old_data(jobs, job):
    profile = SpaceProfile.from_settings(EmbeddingSettings())
    index = IndexResult(profile, ["obsolete Mars chunk"], [unit_vector()])
    enrichment = EnrichmentResult(
        fields=Enrichment(
            summary="old summary", tags=["auto"], category="old category", entities=[]
        ),
        model="fixture",
        prompt_version="test",
    )
    for result in (index, enrichment):
        jobs.publish(job, publish_process, result)
    return index, enrichment


def seed_manual_data(db, note_id, peer_id):
    collection_id = db.execute(
        "INSERT INTO collections(title) VALUES ('manual') RETURNING id"
    ).fetchone()[0]
    db.execute("UPDATE notes SET manual_tags='[\"manual\"]' WHERE id=%s", (note_id,))
    for member in (note_id, peer_id):
        db.execute(
            "INSERT INTO collection_notes(collection_id,note_id) VALUES (%s,%s)",
            (collection_id, member),
        )
    a, b = sorted((note_id, peer_id))
    db.execute("INSERT INTO note_links VALUES (%s,%s,'semantic',0.5,'test',now())", (a, b))


def assert_stale(jobs, occupation, results):
    for result in results:
        with pytest.raises(StaleJob):
            jobs.publish(occupation, publish_process, result, complete=True)
    with pytest.raises(StaleJob):
        jobs.complete(occupation)


@pytest.mark.parametrize("enabled", [True, False])
def test_patch_replaces_active_process_and_preserves_originals(system, db, tmp_path, enabled):
    service, jobs = system
    note = create(service)
    peer = create(NotesService(service.database), "unrelated")
    old = jobs.claim(30)
    results = publish_old_data(jobs, old)
    seed_manual_data(db, note.id, peer.id)
    storage = Storage(tmp_path)
    (key,) = add_files(db, note, storage, ["original"])
    db.execute(
        """UPDATE notes SET ingestion_error='{"code":"old","message":"old"}',
           processing_error='{"code":"old","message":"old"}' WHERE id=%s""",
        (note.id,),
    )

    updated = NotesService(service.database, processing_enabled=enabled).patch(
        note.id, NotePatch(title="Edited title", extracted_text="new body")
    )
    assert updated.id == note.id
    assert (updated.title, updated.extracted_text) == ("Edited title", "new body")
    assert updated.source == note.source
    assert len(updated.attachments) == 1
    assert storage.path(key).read_bytes() == b"original"
    assert updated.processing_status == ("queued" if enabled else "not_requested")
    assert (
        updated.summary
        is updated.category
        is updated.processing_error
        is updated.ingestion_error
        is None
    )
    assert updated.tags == updated.entities == []
    assert db.execute("SELECT manual_tags FROM notes WHERE id=%s", (note.id,)).fetchone()[0] == [
        "manual"
    ]
    assert db.execute("SELECT count(*) FROM collection_notes").fetchone()[0] == 2
    for table in ("chunks", "enrichments", "note_links"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert db.execute(
        "SELECT status,lease_token,lease_until FROM jobs WHERE id=%s", (old.id,)
    ).fetchone() == ("cancelled", None, None)
    assert db.execute("SELECT status FROM jobs WHERE id<>%s", (old.id,)).fetchall() == (
        [("queued",)] if enabled else []
    )
    assert_stale(jobs, old, results)


def test_concurrent_patches_cancel_intermediate_jobs(system, db):
    service, jobs = system
    note = create(service)
    old = jobs.claim(30)
    patches = [NotePatch(title="First title"), NotePatch(extracted_text="Second body")]
    with ThreadPoolExecutor(2) as executor:
        # Both transactions contend for the note row while the original worker is occupied.
        with service.database.transaction() as conn:
            conn.execute("SELECT id FROM notes WHERE id=%s FOR UPDATE", (note.id,))
            futures = [executor.submit(service.patch, note.id, patch) for patch in patches]
            assert jobs.claim(30) is None
        for future in futures:
            assert future.result(timeout=10).processing_status == "queued"

    updated = service.get(note.id)
    assert (updated.title, updated.extracted_text) == ("First title", "Second body")
    assert db.execute(
        """SELECT status,lease_token FROM jobs WHERE note_id=%s
           ORDER BY created_at,id""",
        (note.id,),
    ).fetchall() == [("cancelled", None), ("cancelled", None), ("queued", None)]
    with pytest.raises(StaleJob):
        jobs.complete(old)


def test_delete_hides_before_cleanup_and_removes_only_its_data(system, db, tmp_path):
    service, jobs = system
    note = create(service)
    peer = create(NotesService(service.database), "unrelated")
    old = jobs.claim(30)
    results = publish_old_data(jobs, old)
    seed_manual_data(db, note.id, peer.id)
    storage = Storage(tmp_path)
    (key,) = add_files(db, note, storage, ["original"])
    (peer_key,) = add_files(db, peer, storage, ["original"])
    search = SearchService(service.database, embedder=FixtureEmbedder(EmbeddingSettings()))
    query = SearchRequest(query="Mars")
    assert [item.note_id for item in search.search(query).items] == [note.id]

    service.delete(note.id)
    service.delete(note.id)
    assert search.search(query).items == []
    assert [item.id for item in service.list(NoteListQuery()).items] == [peer.id]
    for operation in (
        lambda: service.get(note.id),
        lambda: service.patch(note.id, NotePatch(title="invisible")),
        lambda: service.retry(note.id, "process"),
    ):
        with pytest.raises(NoteError, match="Заметка не найдена"):
            operation()
    assert_stale(jobs, old, results)
    assert storage.path(key).exists()
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1

    assert Worker(jobs, handlers()).run_once()
    assert not storage.path(key).exists()
    assert storage.path(peer_key).read_bytes() == b"original"
    assert db.execute("SELECT count(*) FROM attachments").fetchone()[0] == 1
    assert db.execute("SELECT note_id FROM sources").fetchall() == [(peer.id,)]
    assert db.execute("SELECT note_id FROM collection_notes").fetchall() == [(peer.id,)]
    for table in ("chunks", "enrichments", "note_links"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert db.execute(
        """SELECT deleted_at IS NOT NULL, extracted_text, summary, tags, manual_tags
           FROM notes WHERE id=%s""",
        (note.id,),
    ).fetchone() == (True, None, None, [], [])
    service.delete(note.id)
    assert db.execute("SELECT status,attempts FROM jobs WHERE kind='cleanup'").fetchall() == [
        ("done", 1)
    ]


def test_cleanup_partial_failure_and_retry(system, db, tmp_path):
    service, jobs = system
    note = create(NotesService(service.database))
    storage = Storage(tmp_path)
    first, second = add_files(db, note, storage, ["a-original", "b-original"])
    # The second key temporarily points to a directory, which must never be removed.
    second_path = storage.path(second)
    second_path.unlink()
    second_path.mkdir()
    sentinel = second_path / "unrelated"
    sentinel.write_bytes(b"preserve")
    service.delete(note.id)

    worker = Worker(jobs, handlers())
    assert worker.run_once()
    assert not storage.path(first).exists()
    assert sentinel.read_bytes() == b"preserve"
    job_id, status, attempts, error = db.execute(
        "SELECT id,status,attempts,error FROM jobs"
    ).fetchone()
    assert (status, attempts, error["code"]) == ("failed", 1, "handler_failed")
    assert db.execute("SELECT count(*) FROM attachments").fetchone()[0] == 2
    sentinel.unlink()
    second_path.rmdir()

    service.delete(note.id)
    service.delete(note.id)
    assert db.execute("SELECT id,status,attempts FROM jobs").fetchall() == [(job_id, "queued", 1)]
    assert worker.run_once()
    assert db.execute("SELECT id,status,attempts FROM jobs").fetchall() == [(job_id, "done", 2)]
    assert db.execute("SELECT count(*) FROM attachments").fetchone()[0] == 0


def test_cleanup_rejects_in_root_alias_to_another_attachment(system, db, tmp_path):
    service, jobs = system
    plain = NotesService(service.database)
    note, peer = create(plain), create(plain)
    storage = Storage(tmp_path)
    (key,) = add_files(db, note, storage, ["original"])
    (peer_key,) = add_files(db, peer, storage, ["original"])
    alias = storage.path(key)
    alias.unlink()
    alias.symlink_to(storage.path(peer_key))
    service.delete(note.id)

    assert Worker(jobs, handlers()).run_once()
    assert db.execute("SELECT status FROM jobs").fetchall() == [("failed",)]
    assert alias.is_symlink()
    assert storage.path(peer_key).read_bytes() == b"original"


def test_cleanup_occupation_expires_and_cannot_publish_or_complete(system, db):
    service, jobs = system
    note = create(NotesService(service.database))
    service.delete(note.id)
    old = jobs.claim(30)
    binding = handlers()["cleanup"]
    db.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second'")
    with pytest.raises(StaleJob):
        jobs.publish(old, binding.writer, None, complete=True)
    assert jobs.recover_expired() == 1
    current = jobs.claim(30)
    assert current.id == old.id and current.lease_token != old.lease_token
    with pytest.raises(StaleJob):
        jobs.publish(old, binding.writer, None)
    with pytest.raises(StaleJob):
        jobs.complete(old)
    assert db.execute("SELECT original_text FROM sources").fetchone() == ("Mars original",)
    assert db.execute("SELECT status,lease_token FROM jobs").fetchone() == (
        "processing",
        current.lease_token,
    )
    jobs.publish(current, binding.writer, None, complete=True)
    assert db.execute("SELECT status FROM jobs").fetchone() == ("done",)


def test_cleanup_keeps_telegram_delivery_deduplication(system, db):
    service, jobs = system
    plain = NotesService(service.database)
    delivery = TelegramDelivery(bot_id=1, update_id=2, chat_id=3, message_id=4)
    note = create(plain, delivery=delivery)
    service.delete(note.id)

    assert Worker(jobs, handlers()).run_once()
    with pytest.raises(NoteError, match="Заметка не найдена"):
        create(plain, delivery=delivery)
    assert db.execute("SELECT id FROM notes").fetchall() == [(note.id,)]
    assert db.execute(
        "SELECT original_text,delivery_scope,delivery_id FROM sources"
    ).fetchone() == ("[deleted]", "1", "2")
