import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from unittest.mock import Mock

import pytest
from aiogram import Bot
from fastapi.testclient import TestClient

from app.api_app import create_app
from app.bot.handlers import create_dispatcher
from app.config import Settings
from app.dto import TextNoteCreate
from app.postgres import Database
from app.repositories.sources import SourcesRepository, TelegramDelivery
from app.services.notes import NoteError, NotesService
from tests.telegram_fixtures import RecordingSession, forwarded, update


@pytest.fixture
def service(db, database_url):
    with Database(database_url) as database:
        yield NotesService(database, processing_enabled=True)


def delivery(update_id=1, bot_id=123):
    return TelegramDelivery(bot_id=bot_id, update_id=update_id, chat_id=42, message_id=update_id)


def counts(db):
    return tuple(
        db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("notes", "sources", "jobs")
    )


ORIGINS = [
    None,
    dict(type="user", date=1700000000, sender_user=dict(id=7, is_bot=False, first_name="Test")),
    dict(type="hidden_user", date=1700000000, sender_user_name="Synthetic"),
    dict(
        type="chat",
        date=1700000000,
        sender_chat=dict(id=-7, type="supergroup", title="Test"),
        author_signature="Author",
    ),
    dict(
        type="channel",
        date=1700000000,
        chat=dict(id=-8, type="channel", title="Test"),
        message_id=5,
        author_signature="Author",
    ),
]


@pytest.mark.parametrize("origin", ORIGINS)
@pytest.mark.parametrize("processing", [True, False])
def test_update_provenance_commit_and_web_visibility(service, db, database_url, origin, processing):
    service.processing_enabled = processing

    # A separate SQL session must see committed source/job before success is sent.
    def on_send(method):
        assert "Заметка сохранена" in method.text
        assert counts(db) == (1, 1, int(processing))

    session = RecordingSession(on_send)
    payload = update(origin=origin, **({} if origin else {"is_automatic_forward": True}))

    async def scenario():
        bot = Bot("123:test-synthetic", session=session)
        dispatcher = create_dispatcher(service, Mock())
        await dispatcher.feed_update(bot, payload)
        await dispatcher.feed_update(bot, payload)
        await bot.session.close()

    asyncio.run(scenario())
    assert session.sent[0].text == session.sent[1].text
    row = db.execute("""SELECT channel,delivery_scope,delivery_id,telegram_chat_id,
                       telegram_message_id,original_text,metadata FROM sources""").fetchone()
    assert row[:6] == ("telegram", "123", "1", 42, 1, payload.message.text)
    note = db.execute("SELECT title, title_is_explicit, extracted_text FROM notes").fetchone()
    assert note == ("Заголовок", False, payload.message.text)
    expected = payload.message.forward_origin
    assert row[6] == (
        {"forward_origin": expected.model_dump(mode="json", exclude_none=True)}
        if origin
        else {"is_automatic_forward": True}
    )
    with TestClient(create_app(Settings(database_url=database_url))) as client:
        page = client.get("/api/v1/notes").json()
        assert page["total"] == 1
        detail = client.get("/api/v1/notes/" + page["items"][0]["id"]).json()
        assert detail["title"] == "Заголовок"
        assert detail["extracted_text"] == detail["source"]["original_text"] == payload.message.text
        assert detail["processing_status"] == ("queued" if processing else "not_requested")


def test_concurrent_delivery_new_send_and_bot_scope(service, db, monkeypatch):
    barrier = Barrier(4)
    original = SourcesRepository.create_telegram_text

    def synchronized(self, *args):
        barrier.wait(timeout=5)
        return original(self, *args)

    request = TextNoteCreate(source_type="text", text="Identical synthetic text")
    with monkeypatch.context() as patch:
        patch.setattr(SourcesRepository, "create_telegram_text", synchronized)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(service.create, request, delivery=delivery()) for _ in range(4)
            ]
            ids = {future.result(timeout=10).id for future in futures}
    assert len(ids) == 1
    assert counts(db) == (1, 1, 1)
    other = service.create(request, delivery=delivery(2))
    another_bot = service.create(request, delivery=delivery(bot_id=456))
    assert len(ids | {other.id, another_bot.id}) == 3
    assert counts(db) == (3, 3, 3)


@pytest.mark.parametrize("failure", ["enqueue", "commit"])
def test_rollback_no_success_and_redelivery(service, db, monkeypatch, failure):
    import app.services.notes as module

    session = RecordingSession()
    original_transaction = service.database.transaction

    @contextmanager
    def failed_commit():
        with original_transaction() as conn:
            yield conn
            raise RuntimeError("synthetic commit failure")

    def fail(*args):
        raise RuntimeError("synthetic enqueue failure")

    async def send():
        bot = Bot("123:test-synthetic", session=session)
        await create_dispatcher(service, Mock()).feed_update(bot, forwarded())
        await bot.session.close()

    with monkeypatch.context() as patch:
        if failure == "enqueue":
            patch.setattr(module, "enqueue_process", fail)
        else:
            patch.setattr(service.database, "transaction", failed_commit)
        asyncio.run(send())
    assert counts(db) == (0, 0, 0)
    assert len(session.sent) == 1 and "Не удалось" in session.sent[0].text
    asyncio.run(send())
    assert counts(db) == (1, 1, 1)
    assert "Заметка сохранена" in session.sent[-1].text


def test_redelivery_after_delete_does_not_resurrect(service, db):
    request = TextNoteCreate(source_type="text", text="Synthetic")
    note = service.create(request, delivery=delivery())
    service.delete(note.id)
    before = counts(db)
    with pytest.raises(NoteError, match="Заметка не найдена"):
        service.create(request, delivery=delivery())
    assert counts(db) == before
