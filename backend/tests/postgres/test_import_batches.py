"""T12 HTTP and Telegram acceptance against PostgreSQL; no model services."""

import asyncio
from io import BytesIO
from threading import Barrier
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from aiogram import Bot
from aiogram.methods import GetFile
from aiogram.types import File, User
from fastapi.testclient import TestClient

from app.api_app import create_app
from app.bot import __main__ as entry
from app.bot.handlers import create_dispatcher
from app.bot.settings import BotSettings
from app.config import Settings
from app.errors import ProcessingError
from app.extraction import ExtractHandler, publish_extract
from app.jobs_repository import JobsRepository
from app.postgres import Database
from app.repositories.imports import ImportsRepository
from app.repositories.sources import SourcesRepository, TelegramDelivery
from app.services.file_import import FileImportService
from app.services.imports import ImportFile, ImportsService
from app.services.notes import NotesService
from app.services.storage import Storage
from tests.postgres.test_file_import import worker
from tests.telegram_fixtures import RecordingSession, update

API = "/api/v1"


@pytest.fixture
def test_settings(db, database_url, tmp_path, monkeypatch):
    settings = Settings(
        database_url=database_url,
        data_dir=tmp_path,
        processing_enabled=True,
        text_file_max_bytes=32,
    )
    monkeypatch.setenv("NOTE_AGENT_SEARCH_DENSE_ENABLED", "false")
    for key in ("database_url", "data_dir", "processing_enabled", "text_file_max_bytes"):
        monkeypatch.setenv(f"NOTE_AGENT_{key.upper()}", str(getattr(settings, key)))
    return settings


@pytest.fixture
def client(test_settings):
    with TestClient(create_app(test_settings), raise_server_exceptions=False) as client:
        yield client


def upload(client, *entries, **collection):
    return client.post(
        f"{API}/imports", files=[("files", entry) for entry in entries], data=collection
    )


def get(client, path):
    response = client.get(f"{API}{path}")
    assert response.status_code == 200, response.text
    return response.json()


def originals(settings):
    return list((settings.data_dir / "uploads").rglob("original"))


def test_mixed_batch_progress_retry_and_removed_notes(client, test_settings, db):
    response = upload(
        client,
        ("ok.md", b"# valid"),
        ("wrong.pdf", b"pdf"),
        ("encoding.txt", b"\xff"),
        ("big.txt", b"x" * 33),
        collection_title="Тест",
    )
    assert response.status_code == 202, response.text
    receipt = response.json()
    path = "/imports/" + receipt["batch_id"]
    collection = "/collections/" + receipt["collection_id"]
    state = get(client, path)
    assert state["status"] == "processing"
    assert (state["total"], state["succeeded"], state["failed"]) == (4, 0, 2)
    assert [item["id"] for item in state["items"]] == receipt["item_ids"]
    assert [item["status"] for item in state["items"]] == ["queued", "failed", "queued", "failed"]
    assert state["items"][1]["error"]["code"] == "unsupported_format"
    assert state["items"][3]["error"]["code"] == "file_too_large"
    good_id, bad_id = (state["items"][i]["note_id"] for i in (0, 2))
    assert get(client, collection)["title"] == "Тест"
    assert get(client, collection + "?limit=1")["notes"]["total"] == 2
    first = get(client, collection + "?limit=1")["notes"]["items"][0]["id"]
    second = get(client, collection + "?limit=1&offset=1")["notes"]["items"][0]["id"]
    assert {first, second} == {good_id, bad_id}

    with Database(test_settings.database_url) as database:
        jobs = JobsRepository(database)
        active = jobs.claim(60)
        assert active.kind == "extract"
        by_note = {item["note_id"]: item for item in get(client, path)["items"]}
        assert by_note[str(active.note_id)]["status"] == "processing"

        class Context:
            def publish(self, result):
                jobs.publish(active, publish_extract, result, complete=True)

        try:
            ExtractHandler(test_settings.database_url, test_settings)(active, Context())
        except ProcessingError as exc:
            jobs.fail(active, exc.code, exc.message)
        assert worker(jobs).run_once()
        assert get(client, "/notes/" + good_id)["processing_status"] == "queued"
        assert get(client, path)["items"][0]["status"] == "done"

        failed = get(client, path)
        assert (failed["status"], failed["succeeded"], failed["failed"]) == ("done", 1, 3)
        assert failed["items"][2]["error"]["code"] == "invalid_encoding"
        retry = client.post(f"{API}/notes/{bad_id}/retry", json={"stage": "extract"})
        assert retry.status_code == 202
        state = get(client, path)
        assert (state["status"], state["succeeded"], state["failed"]) == ("processing", 1, 2)
        assert state["items"][2]["status"] == "queued" and state["items"][2]["error"] is None
        while worker(jobs).run_once():
            pass
        assert db.execute(
            "SELECT attempts FROM jobs WHERE note_id=%s AND kind='extract'", (bad_id,)
        ).fetchall() == [(2,)]
        assert get(client, path)["failed"] == 3

        for note_id in (good_id, bad_id):
            assert client.delete(f"{API}/notes/{note_id}").status_code == 204
        assert get(client, collection)["notes"]["total"] == 0
        while worker(jobs).run_once():
            pass
        final = get(client, path)
        assert final["items"][0]["status"] == "done"
        assert final["items"][2]["error"]["code"] == "invalid_encoding"
        assert all(item["note_id"] is None for item in final["items"])
        assert not originals(test_settings)


def test_retry_recovers_missing_original(client, test_settings, db):
    accepted = upload(client, ("retry.md", b"# recoverable"))
    assert accepted.status_code == 202
    path = "/imports/" + accepted.json()["batch_id"]
    original = originals(test_settings)[0]
    temporary = original.rename(original.with_name("temporarily-unavailable"))
    with Database(test_settings.database_url) as database:
        runner = worker(JobsRepository(database))
        assert runner.run_once()
        failed = get(client, path)
        assert (failed["status"], failed["failed"]) == ("failed", 1)
        note_id = failed["items"][0]["note_id"]
        assert failed["items"][0]["error"]["code"] == "original_missing"
        temporary.rename(original)
        response = client.post(f"{API}/notes/{note_id}/retry", json={"stage": "extract"})
        assert response.status_code == 202
        queued = get(client, path)
        assert (queued["status"], queued["failed"]) == ("queued", 0)
        assert runner.run_once()
        done = get(client, path)
        assert (done["status"], done["succeeded"], done["failed"]) == ("done", 1, 0)
        assert done["items"][0]["note_id"] == note_id
        assert db.execute(
            "SELECT attempts FROM jobs WHERE note_id=%s AND kind='extract'", (note_id,)
        ).fetchall() == [(2,)]


def test_collection_lifecycle_and_batch_into_existing(client, db):
    created = client.post(f"{API}/collections", json={"title": "Учёба"})
    assert created.status_code == 201
    collection_id = created.json()["id"]
    url = f"{API}/collections/{collection_id}"
    assert [row["title"] for row in get(client, "/collections")] == ["Учёба"]
    result = upload(client, ("one.txt", b"text"), collection_id=collection_id)
    assert result.status_code == 202, result.text
    batch_path = "/imports/" + result.json()["batch_id"]
    first = get(client, batch_path)
    assert (first["status"], first["succeeded"], first["failed"]) == ("queued", 0, 0)
    assert first["collection_id"] == collection_id
    text = client.post(f"{API}/notes", json={"source_type": "text", "text": "Вторая заметка"})
    note_id = text.json()["id"]
    assert (
        client.post(url + "/notes", json={"note_ids": [note_id, str(uuid4())]}).status_code == 404
    )
    assert get(client, f"/collections/{collection_id}")["notes"]["total"] == 1
    for _ in range(2):
        assert client.post(url + "/notes", json={"note_ids": [note_id, note_id]}).status_code == 204
    assert get(client, f"/collections/{collection_id}")["notes"]["total"] == 2
    scoped_query = {"query": "Вторая заметка", "collection_id": collection_id}
    search = client.post(f"{API}/search", json=scoped_query)
    assert search.status_code == 200
    assert [row["note_id"] for row in search.json()["items"]] == [note_id]
    for _ in range(2):
        assert client.delete(url + f"/notes/{note_id}").status_code == 204
    assert get(client, f"/collections/{collection_id}")["notes"]["total"] == 1
    search = client.post(f"{API}/search", json=scoped_query)
    assert search.status_code == 200 and search.json()["items"] == []
    assert client.delete(f"{API}/notes/{note_id}").status_code == 204
    assert client.post(url + "/notes", json={"note_ids": [note_id]}).status_code == 404
    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404
    assert get(client, batch_path)["collection_id"] is None
    imported_note = f"{API}/notes/{first['items'][0]['note_id']}"
    assert client.get(imported_note).status_code == 200
    assert db.execute("SELECT count(*) FROM collection_notes").fetchone() == (0,)
    assert client.delete(imported_note).status_code == 204
    removed = get(client, batch_path)
    assert (removed["status"], removed["succeeded"], removed["failed"]) == ("failed", 0, 1)
    assert removed["items"][0]["note_id"] is None
    assert removed["items"][0]["error"]["code"] == "note_deleted"


def test_limits_invalid_and_automatic_collection(client, test_settings, db):
    file = ("x.txt", b"x")
    assert upload(client, file, collection_id=str(uuid4()), collection_title="X").status_code == 422
    assert upload(client, file, collection_id=str(uuid4())).status_code == 404
    assert upload(client, *([file] * 21)).status_code == 422
    assert upload(client, ("x.txt", b"X" * (10 * 1024 * 1024 + 1))).status_code == 413
    assert upload(client, ("x.txt", b"X" * (11 * 1024 * 1024))).status_code == 413
    assert (
        client.post(
            f"{API}/imports",
            content=b'--b\r\nContent-Disposition: form-data; name="files"; filename="x.txt"\r\n\r\nx',
            headers={"content-type": "multipart/form-data; boundary=b"},
        ).status_code
        == 422
    )
    assert client.get(f"{API}/imports/{uuid4()}").status_code == 404
    assert not originals(test_settings)
    assert db.execute("SELECT count(*) FROM collections").fetchone() == (0,)
    result = upload(client, ("x.txt", b""))
    assert result.status_code == 202
    detail = get(client, "/imports/" + result.json()["batch_id"])
    assert (detail["status"], detail["succeeded"], detail["failed"]) == ("failed", 0, 1)
    assert detail["items"][0]["error"]["code"] == "empty_file"
    assert get(client, "/collections/" + detail["collection_id"])["title"].startswith("Импорт ")


def test_rollback_compensates_originals_and_isolates_item(client, test_settings, db, monkeypatch):
    original_save, original_remove = Storage.save, Storage.remove
    original_create = SourcesRepository.create_file
    saves = []

    def outside_transaction(operation):
        def checked(*args):
            assert db.execute(
                """SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
                   AND pid<>pg_backend_pid() AND state='idle in transaction'"""
            ).fetchone() == (0,)
            return operation(*args)

        return checked

    def fail_on_first(self, note_id, source_type):
        saves.append(note_id)
        if len(saves) == 1:
            raise RuntimeError("synthetic file insert error")
        return original_create(self, note_id, source_type)

    def fail_item(*args):
        raise RuntimeError("synthetic batch persistence failure")

    monkeypatch.setattr(Storage, "save", outside_transaction(original_save))
    monkeypatch.setattr(Storage, "remove", outside_transaction(original_remove))
    with monkeypatch.context() as patch:
        patch.setattr(ImportsRepository, "create_item", fail_item)
        assert upload(client, ("rollback.txt", b"abc")).status_code == 500
    assert not originals(test_settings)
    assert counts(db) == (0, 0, 0, 0, 0)
    monkeypatch.setattr(SourcesRepository, "create_file", fail_on_first)
    accepted = upload(client, ("fail.txt", b"first"), ("ok.md", b"second"))
    assert accepted.status_code == 202, accepted.text
    state = get(client, "/imports/" + accepted.json()["batch_id"])
    assert state["items"][0]["error"]["code"] == "file_import_failed"
    assert state["items"][1]["status"] == "queued"
    assert counts(db) == (1, 1, 1, 1, 2)
    assert [path.read_bytes() for path in originals(test_settings)] == [b"second"]


def counts(db):
    return tuple(
        db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("notes", "jobs", "collections", "import_batches", "import_items")
    )


class DocumentSession(RecordingSession):
    def __init__(self, on_send=None):
        super().__init__(on_send)
        self.downloaded = []

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="fixture", file_path=method.file_id)
        return await super().make_request(bot, method, timeout)

    async def stream_content(self, url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        self.downloaded.append(name)
        if name == "large":
            yield b"x" * 33
        else:
            yield b"# synthetic"


def document(update_id, *, chat_id=42, group=None, name="notes.md", remote=None):
    return update(
        update_id,
        chat={"id": chat_id, "type": "private"},
        text=None,
        media_group_id=group,
        document={
            "file_id": remote or str(update_id),
            "file_unique_id": "fixture",
            "file_name": name,
        },
    )


def test_telegram_documents_groups_replays_and_scopes(test_settings, db):
    async def scenario(database):
        first, second = DocumentSession(), DocumentSession()
        bot = Bot("123:synthetic", session=first)
        another_bot = Bot("456:synthetic", session=second)
        imports = ImportsService(FileImportService(database, test_settings))
        dispatcher = create_dispatcher(NotesService(database), Mock(), imports, group_window=0.02)
        for payload in (document(1), document(1), document(2, remote="large")):
            await dispatcher.feed_update(bot, payload)
        for payload in (
            document(3, group="same"),
            document(3, group="same"),
            document(4, group="same", name="bad.pdf"),
            document(5, group="same", chat_id=43),
        ):
            await dispatcher.feed_update(bot, payload)
        await dispatcher.feed_update(another_bot, document(3, group="same"))
        # Observe real timer flush, then deliver a late member and a replay of the group.
        async with asyncio.timeout(3):
            while len(first.sent) < 5 or not second.sent:
                await asyncio.sleep(0.005)
        for payload in (document(3, group="same"), document(6, group="same")):
            await dispatcher.feed_update(bot, payload)
        await dispatcher.emit_shutdown(bot=bot)
        assert first.downloaded[:2] == ["1", "large"]
        assert sorted(first.downloaded[2:]) == ["3", "5", "6"]
        assert second.downloaded == ["3"]
        assert first.sent[0].text == first.sent[1].text
        await first.close()
        await second.close()

    with Database(test_settings.database_url) as database:
        asyncio.run(scenario(database))
    rows = db.execute(
        "SELECT delivery_scope, external_id, total FROM import_batches ORDER BY 1,2"
    ).fetchall()
    assert rows == [
        ("123:42", "group:same", 3),
        ("123:42", "update:1", 1),
        ("123:42", "update:2", 1),
        ("123:43", "group:same", 1),
        ("456:42", "group:same", 1),
    ]
    assert counts(db) == (5, 5, 5, 5, 7)
    assert db.execute("SELECT count(*) FROM collection_notes").fetchone() == (5,)
    assert db.execute(
        "SELECT status, error->>'code' FROM import_items WHERE note_id IS NULL ORDER BY 2"
    ).fetchall() == [("failed", "file_too_large"), ("failed", "unsupported_format")]


def test_concurrent_telegram_admission_only_commits_once(test_settings, db, monkeypatch):
    real_save = Storage.save
    barrier = Barrier(2)

    def save(*args):
        result = real_save(*args)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(Storage, "save", save)

    async def race(imports):
        async def submit():
            delivery = TelegramDelivery(bot_id=123, update_id=1, chat_id=42, message_id=1)
            with BytesIO(b"abc") as source:
                return await asyncio.to_thread(
                    imports.accept_telegram,
                    [ImportFile("x.md", 3, source, delivery)],
                    "123:42",
                    "group:g",
                )

        ids = await asyncio.gather(submit(), submit())
        assert ids[0] == ids[1]

    with Database(test_settings.database_url) as database:
        asyncio.run(race(ImportsService(FileImportService(database, test_settings))))
    assert counts(db) == (1, 1, 1, 1, 1)
    assert len(originals(test_settings)) == 1


def test_telegram_polling_shutdown_flushes_before_closing(test_settings, db, monkeypatch):
    def require_commit(_):
        assert counts(db) == (2, 2, 1, 1, 2)

    session = DocumentSession(on_send=require_commit)
    bot = Bot("123:synthetic", session=session)
    monkeypatch.setattr(
        bot, "me", AsyncMock(return_value=User(id=123, is_bot=True, first_name="Test"))
    )
    monkeypatch.setattr(entry, "Bot", lambda _: bot)
    actual_close = Database.close

    def close(self):
        assert len(session.sent) == 1, "The group must finish before closing the pool"
        assert not session.closed
        return actual_close(self)

    monkeypatch.setattr(Database, "close", close)

    async def scenario():
        ready = asyncio.Event()
        dispatchers = []

        async def updates(*args, **kwargs):
            yield document(1, group="shutdown")
            yield document(2, group="shutdown")
            ready.set()
            await asyncio.Future()

        def dispatcher(notes, chat, imports):
            instance = create_dispatcher(notes, chat, imports, group_window=60)
            monkeypatch.setattr(instance, "_listen_updates", updates)
            dispatchers.append(instance)
            return instance

        monkeypatch.setattr(entry, "create_dispatcher", dispatcher)
        polling = asyncio.create_task(entry.run(BotSettings(token="123:synthetic"), test_settings))
        await asyncio.wait_for(ready.wait(), 5)
        assert not session.sent
        await asyncio.wait_for(dispatchers[0].stop_polling(), 5)
        await asyncio.wait_for(polling, 5)

    asyncio.run(scenario())
    assert session.closed
    assert counts(db) == (2, 2, 1, 1, 2)
