import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot

from app.bot import __main__ as entry
from app.bot.handlers import create_dispatcher
from app.bot.settings import BotSettings
from app.config import Settings
from tests.telegram_fixtures import RecordingSession, forwarded, update


def test_dispatch_text_and_unsupported():
    notes = Mock()
    notes.create.return_value = SimpleNamespace(id="saved")
    session = RecordingSession()

    async def scenario():
        bot = Bot("123:test-synthetic", session=session)
        dispatcher = create_dispatcher(notes, Mock())
        await dispatcher.feed_update(bot, forwarded())
        await dispatcher.feed_update(
            bot,
            update(
                2, text=None, voice={"file_id": "fake", "file_unique_id": "fake", "duration": 1}
            ),
        )
        await dispatcher.feed_update(
            bot,
            update(
                3,
                text=None,
                caption="Caption",
                photo=[{"file_id": "fake", "file_unique_id": "fake", "width": 1, "height": 1}],
            ),
        )
        await bot.session.close()

    asyncio.run(scenario())
    notes.create.assert_called_once()
    request = notes.create.call_args.args[0]
    assert request.text == "  Заголовок\nТекст  "
    assert request.title is None
    assert notes.create.call_args.kwargs["delivery"].bot_id == 123
    assert len(session.sent) == 1 and session.sent[0].text == "Заметка сохранена: saved"
    assert session.closed


def test_save_failure_never_success():
    notes = Mock()
    notes.create.side_effect = RuntimeError("private contents")
    session = RecordingSession()

    async def scenario():
        bot = Bot("123:test-synthetic", session=session)
        await create_dispatcher(notes, Mock()).feed_update(bot, forwarded())
        await bot.session.close()

    asyncio.run(scenario())
    assert len(session.sent) == 1
    assert "Не удалось" in session.sent[0].text
    assert "private" not in session.sent[0].text


def test_cancel_waits_for_database_thread():
    entered, release, finished = Event(), Event(), Event()

    def save(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        finished.set()
        return SimpleNamespace(id="saved")

    async def scenario():
        bot = Bot("123:test-synthetic", session=RecordingSession())
        task = asyncio.create_task(
            create_dispatcher(SimpleNamespace(create=save), Mock()).feed_update(bot, forwarded())
        )
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert not bot.session.sent
        await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [None, "open", "poll"])
def test_lifecycle_closes_session_and_pool(monkeypatch, failure):
    database = Mock()
    if failure == "open":
        database.open.side_effect = RuntimeError("open")
    dispatcher = Mock()
    dispatcher.start_polling = AsyncMock(
        side_effect=RuntimeError("poll") if failure == "poll" else None
    )
    session = RecordingSession()
    bot = Bot("123:test-synthetic", session=session)
    monkeypatch.setattr(entry, "Bot", lambda token: bot)
    monkeypatch.setattr(entry, "Database", lambda url: database)
    monkeypatch.setattr(entry, "create_dispatcher", lambda *args, **kw: dispatcher)
    call = entry.run(BotSettings(token="123:test-synthetic"), Settings(database_url="fake"))
    if failure:
        with pytest.raises(RuntimeError):
            asyncio.run(call)
    else:
        asyncio.run(call)
        kwargs = dispatcher.start_polling.call_args.kwargs
        assert kwargs == dict(
            allowed_updates=["message"], handle_as_tasks=False, close_bot_session=False
        )
    database.close.assert_called_once()
    assert session.closed


def test_settings_token_is_secret_and_required(monkeypatch):
    monkeypatch.delenv("NOTE_AGENT_TELEGRAM_TOKEN", raising=False)
    with pytest.raises(ValueError):
        BotSettings.from_env()
    monkeypatch.setenv("NOTE_AGENT_TELEGRAM_TOKEN", "123:test-synthetic")
    settings = BotSettings.from_env()
    assert "test-synthetic" not in repr(settings)


def test_real_polling_shutdown_drains_save_before_pool_close(monkeypatch):
    from aiogram.types import User

    entered, release, finished = Event(), Event(), Event()
    database = Mock()
    database.close.side_effect = lambda: finished.is_set() or pytest.fail("Pool closed before save")
    session = RecordingSession()
    bot = Bot("123:test-synthetic", session=session)
    monkeypatch.setattr(
        bot,
        "me",
        AsyncMock(
            return_value=User(id=123, is_bot=True, first_name="Synthetic", username="synthetic_bot")
        ),
    )

    def save(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        finished.set()
        return SimpleNamespace(id="saved")

    dispatcher = create_dispatcher(SimpleNamespace(create=save), Mock())

    async def updates(*args, **kwargs):
        yield forwarded()
        await asyncio.Future()

    monkeypatch.setattr(dispatcher, "_listen_updates", updates)
    monkeypatch.setattr(entry, "Bot", lambda token: bot)
    monkeypatch.setattr(entry, "Database", lambda url: database)
    monkeypatch.setattr(entry, "create_dispatcher", lambda *args, **kwargs: dispatcher)

    async def scenario():
        task = asyncio.create_task(
            entry.run(BotSettings(token="123:test-synthetic"), Settings(database_url="fake"))
        )
        assert await asyncio.to_thread(entered.wait, 5)
        stopping = asyncio.create_task(dispatcher.stop_polling())
        await asyncio.sleep(0)
        assert not session.closed and not database.close.called
        release.set()
        await asyncio.wait_for(stopping, 5)
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    database.close.assert_called_once()
    assert session.closed
