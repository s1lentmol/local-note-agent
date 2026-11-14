"""T15 with real decoder inputs, PostgreSQL and replaceable model adapters."""

import asyncio
import time
import wave
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from threading import Thread
from unittest.mock import Mock

import av
import psycopg
import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetFile
from fastapi.testclient import TestClient

from app.api_app import create_app
from app.bot.handlers import create_dispatcher
from app.extraction import ExtractHandler, publish_extract
from app.job_runner.settings import WorkerSettings
from app.model_clients.contracts import ModelClientError, Transcript
from app.model_clients.testing import FakeTranscriber
from app.services.imports import ImportsService
from app.services.notes import NotesService
from tests.postgres.test_file_import import system as file_import_system
from tests.postgres.test_file_import import worker
from tests.postgres.test_import_batches import DocumentSession
from tests.telegram_fixtures import forwarded, update

TEXT = "Космическая экспедиция\n\n  Полный транскрипт.\n"
RESULT = Transcript(text=TEXT, language="ru")
API = "/api/v1"
audio_system = file_import_system


def wav(seconds=1):
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", ""))
        audio.writeframes(b"\0\0" * (seconds * 16000))
    return output.getvalue()


def encoded(container, encoder):
    output = BytesIO()
    with av.open(output, "w", format=container) as audio:
        stream = audio.add_stream(encoder, rate=48000)
        stream.layout = "mono"
        frame = av.AudioFrame(format="s16", layout="mono", samples=4800)
        frame.sample_rate = 48000
        frame.planes[0].update(bytes(frame.planes[0].buffer_size))
        for packet in stream.encode(frame) + stream.encode(None):
            audio.mux(packet)
    return output.getvalue()


def upload(client, data=None, name="record.bin"):
    response = client.post(
        f"{API}/notes/import",
        data={"source_type": "audio"},
        files={"file": (name, wav() if data is None else data, "text/plain")},
    )
    assert response.status_code == 202, response.text
    return response.json()


def runner(jobs, transcriber=None, settings=None):
    run = worker(jobs)
    binding = run.handlers["extract"]
    handler = binding.compute
    run.handlers["extract"] = replace(
        binding,
        compute=replace(handler, settings=settings or handler.settings, transcriber=transcriber),
    )
    return run


@dataclass(frozen=True)
class CheckedTranscriber:
    database_url: str
    audio: bytes

    def transcribe(self, path):
        assert path.read_bytes() == self.audio
        with psycopg.connect(self.database_url, autocommit=True) as conn:
            assert conn.execute(
                """SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
                   AND state IN ('idle in transaction', 'idle in transaction (aborted)')"""
            ).fetchone() == (0,)
        return RESULT


@pytest.mark.parametrize(
    "container,codec", [("ogg", "libopus"), (None, None), ("mp3", "libmp3lame")]
)
def test_audio_extract_process_search_original(audio_system, db, monkeypatch, container, codec):
    _, notes, jobs, settings = audio_system
    monkeypatch.setenv("NOTE_AGENT_SEARCH_DENSE_ENABLED", "false")
    original = encoded(container, codec) if container else wav()
    with TestClient(create_app(settings)) as client:
        note = upload(client, original, "mislabeled.bin")
        note_id = note["id"]
        download = note["attachments"][0]["download_url"]
        assert note["source"]["type"] == "audio"
        assert note["ingestion_status"] == "queued"
        assert client.get(download).content == original

        run = runner(jobs, CheckedTranscriber(settings.database_url, original))
        assert run.run_once()
        ready = notes.get(note_id)
        assert ready.extracted_text == TEXT
        assert ready.source.metadata == {"language": "ru"}
        assert ready.ingestion_status == "ready" and ready.processing_status == "queued"
        assert db.execute("SELECT kind,status FROM jobs ORDER BY created_at").fetchall() == [
            ("extract", "done"),
            ("process", "queued"),
        ]
        assert run.run_once()
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] > 0
        search = client.post(
            f"{API}/search", json={"query": "Космическая экспедиция", "source_type": "audio"}
        )
        assert [item["note_id"] for item in search.json()["items"]] == [note_id]
        assert client.get(download).content == original
        assert client.delete(f"{API}/notes/{note_id}").status_code == 204
        assert client.get(download).status_code == 404
        assert run.run_once()
        assert not list(settings.data_dir.rglob("original"))
        assert db.execute("SELECT count(*) FROM chunks").fetchone() == (0,)


@dataclass(frozen=True)
class FailedTranscriber:
    def transcribe(self, path):
        raise ModelClientError("private provider detail")


@pytest.mark.parametrize(
    "scenario,code",
    [
        ("corrupt", "invalid_audio"),
        ("truncated_ogg", "invalid_audio"),
        ("codec", "invalid_audio"),
        ("long", "audio_too_long"),
        ("empty", "empty_asr_result"),
        ("transcript_limit", "text_too_large"),
        ("unavailable", "asr_unavailable"),
    ],
)
def test_audio_failures_no_publication(audio_system, db, scenario, code):
    _, notes, jobs, settings = audio_system
    original = {
        "corrupt": b"OggS invalid data",
        "long": wav(2),
    }.get(scenario)
    if scenario == "codec":
        original = encoded("wav", "adpcm_ima_wav")
    if scenario == "truncated_ogg":
        original = encoded("ogg", "libopus")[:-1]
    settings = settings.model_copy(update={"audio_max_seconds": 1})
    transcriber = FailedTranscriber()
    if scenario in {"empty", "transcript_limit"}:
        text = " \n " if scenario == "empty" else "x" * 100001
        transcriber = FakeTranscriber(Transcript(text=text, language="ru"))
    with TestClient(create_app(settings)) as client:
        note = upload(client, original, "fixture.ogg")
        assert runner(jobs, transcriber, settings).run_once()
        failed = notes.get(note["id"])
        assert failed.ingestion_error.code == code
        assert "private" not in failed.ingestion_error.message
        assert failed.extracted_text is None and failed.source.metadata == {}
        assert db.execute("SELECT kind, status FROM jobs").fetchall() == [("extract", "failed")]
        assert client.get(note["attachments"][0]["download_url"]).content == (original or wav())
        if scenario == "unavailable":
            retry = client.post(f"{API}/notes/{note['id']}/retry", json={"stage": "extract"})
            assert retry.status_code == 202
            assert runner(jobs, FakeTranscriber(RESULT), settings).run_once()
            assert notes.get(note["id"]).extracted_text == TEXT
            assert db.execute("SELECT attempts FROM jobs WHERE kind='extract'").fetchone() == (2,)


def test_audio_size_and_mixed_batch(audio_system, db):
    _, _, _, settings = audio_system
    audio = b"x" * (10 * 1024 * 1024 + 1)
    with TestClient(create_app(settings)) as client:
        receipt = client.post(
            f"{API}/imports",
            files=[
                ("files", ("audio.OGG", audio, "application/octet-stream")),
                ("files", ("note.txt", b"ordinary note", "audio/mpeg")),
            ],
        )
        assert receipt.status_code == 202, receipt.text
        batch = client.get(f"{API}/imports/{receipt.json()['batch_id']}").json()
        assert len(batch["items"]) == 2 and all(item["note_id"] for item in batch["items"])
        assert db.execute("SELECT type FROM sources ORDER BY type").fetchall() == [
            ("audio",),
            ("text_file",),
        ]
        assert db.execute("SELECT count(*) FROM collection_notes").fetchone() == (2,)
    reduced = settings.model_copy(update={"audio_max_bytes": 4})
    with TestClient(create_app(reduced)) as client:
        response = client.post(
            f"{API}/notes/import",
            data={"source_type": "audio"},
            files={"file": ("x.ogg", b"x" * 5)},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "file_too_large"


@dataclass(frozen=True)
class BlockedTranscriber:
    signals: Path

    def transcribe(self, path):
        (self.signals / "asr-started").touch()
        while not (self.signals / "asr-finish").exists():
            time.sleep(0.01)
        return RESULT


@pytest.mark.parametrize("interruption", ["delete", "expired", "reclaimed"])
def test_audio_interruption(audio_system, db, interruption):
    _, notes, jobs, settings = audio_system
    run = runner(jobs, BlockedTranscriber(settings.data_dir))
    run.settings = WorkerSettings(lease_seconds=12, heartbeat_seconds=10, timeout_seconds=20)
    thread = Thread(target=run.run_once)
    with TestClient(create_app(settings)) as client:
        note = upload(client)
        thread.start()
        try:
            deadline = time.monotonic() + 7
            while not (settings.data_dir / "asr-started").exists():
                assert time.monotonic() < deadline and thread.is_alive()
                time.sleep(0.01)
            if interruption == "delete":
                assert client.delete(f"{API}/notes/{note['id']}").status_code == 204
            else:
                db.execute("UPDATE jobs SET lease_until=clock_timestamp() WHERE kind='extract'")
                if interruption == "reclaimed":
                    assert jobs.recover_expired() == 1
                    current = jobs.claim(30)
        finally:
            (settings.data_dir / "asr-finish").touch()
            thread.join(10)
        assert not thread.is_alive()
        assert db.execute("SELECT extracted_text FROM notes").fetchone() == (None,)
        assert db.execute("SELECT metadata FROM sources").fetchone() == ({},)
        assert db.execute("SELECT count(*) FROM jobs WHERE kind='process'").fetchone() == (0,)
        if interruption == "delete":
            assert db.execute("SELECT status FROM jobs WHERE kind='extract'").fetchone() == (
                "cancelled",
            )
            assert run.run_once() and not list(settings.data_dir.rglob("original"))
        elif interruption == "reclaimed":
            assert db.execute("SELECT lease_token FROM jobs").fetchone() == (current.lease_token,)
            jobs.publish(
                current,
                publish_extract,
                ExtractHandler(settings.database_url, settings, FakeTranscriber(RESULT)).extract(
                    next(settings.data_dir.rglob("original")), "audio"
                ),
                complete=True,
            )
            assert notes.get(note["id"]).extracted_text == TEXT


def test_telegram_audio_delivery_commit_and_provenance(audio_system, db):
    files, notes, jobs, settings = audio_system
    original = encoded("ogg", "libopus")

    class AudioSession(DocumentSession):
        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, GetFile) and method.file_id == "too-big":
                raise TelegramBadRequest(method, "Bad Request: file is too big")
            return await super().make_request(bot, method, timeout)

        async def stream_content(self, url, **kwargs):
            self.downloaded.append(url.rsplit("/", 1)[-1])
            yield original

    def verify_commit(method):
        batch_id = (
            method.text.split()[3].rstrip(".")
            if method.text.startswith("Импорт")
            else method.text.split()[-1].rstrip(".")
        )
        assert db.execute(
            "SELECT count(*) FROM import_batches WHERE id=%s", (batch_id,)
        ).fetchone() == (1,)

    async def scenario():
        session = AudioSession(verify_commit)
        bot = Bot("123:synthetic", session=session)
        imports = ImportsService(files)
        dispatcher = create_dispatcher(NotesService(files.database), Mock(), imports)
        voice = {
            "file_id": "same-remote-file",
            "file_unique_id": "same",
            "duration": 1,
            "mime_type": "audio/ogg",
        }
        first = update(
            1, text=None, voice=voice, origin=forwarded().message.forward_origin.model_dump()
        )
        for payload in (
            first,
            first,
            update(
                2, text=None, voice=voice, origin=forwarded().message.forward_origin.model_dump()
            ),
            update(3, text=None, media_group_id="group", audio={**voice, "file_name": "song.opus"}),
            update(
                4,
                text=None,
                media_group_id="group",
                document={"file_id": "document", "file_unique_id": "doc", "file_name": "song.ogg"},
            ),
            update(
                5,
                text=None,
                voice={**voice, "file_id": "too-big"},
                origin=forwarded().message.forward_origin.model_dump(),
            ),
            update(
                6,
                text=None,
                voice={**voice, "file_size": settings.audio_max_bytes + 1},
                origin=forwarded().message.forward_origin.model_dump(),
            ),
        ):
            await dispatcher.feed_update(bot, payload)
        await dispatcher.emit_shutdown(bot=bot)
        assert session.downloaded.count("same-remote-file") == 3
        assert session.downloaded.count("document") == 1
        assert len(session.sent) == 6
        assert session.sent[0].text == session.sent[1].text
        assert any("Telegram Bot API" in reply.text for reply in session.sent)
        assert any("допустимый размер" in reply.text for reply in session.sent)
        await session.close()

    asyncio.run(scenario())
    assert db.execute(
        "SELECT error->>'code' FROM import_items WHERE status='failed' ORDER BY 1"
    ).fetchall() == [("file_too_large",), ("telegram_file_too_large",)]
    assert db.execute("SELECT count(*) FROM import_items").fetchone() == (6,)
    assert db.execute("SELECT count(*) FROM jobs").fetchone() == (4,)
    assert db.execute("SELECT count(*) FROM collection_notes").fetchone() == (4,)

    run = runner(jobs, CheckedTranscriber(settings.database_url, original))
    while run.run_once():
        pass
    rows = db.execute(
        """SELECT note_id,type,delivery_scope,delivery_id,telegram_chat_id,telegram_message_id,
                  metadata FROM sources WHERE channel='telegram' ORDER BY delivery_id"""
    ).fetchall()
    assert len(rows) == 4
    with TestClient(create_app(settings)) as client:
        for i, (note_id, source_type, scope, delivery, chat, message, metadata) in enumerate(
            rows, 1
        ):
            assert (source_type, scope, delivery, chat, message) == ("audio", "123", str(i), 42, i)
            assert metadata["language"] == "ru"
            assert metadata["telegram_media_type"] == ["voice", "voice", "audio", "document"][i - 1]
            if i == 1:
                assert metadata["forward_origin"]["sender_user_name"] == "Synthetic"
            if i >= 3:
                assert metadata["media_group_id"] == "group"
            ready = notes.get(note_id)
            assert ready.extracted_text == TEXT
            assert client.get(ready.attachments[0].download_url).content == original


def test_search_preview_from_long_audio_transcript(audio_system, monkeypatch):
    _, _, jobs, settings = audio_system
    monkeypatch.setenv("NOTE_AGENT_SEARCH_DENSE_ENABLED", "false")
    text = " " + "Космическая экспедиция " * 300
    with TestClient(create_app(settings)) as client:
        note = upload(client)
        run = runner(jobs, FakeTranscriber(Transcript(text=text, language="ru")))
        assert run.run_once() and run.run_once()
        search = client.post("/api/v1/search", json={"query": "экспедиция", "source_type": "audio"})
        assert search.status_code == 200, search.text
        assert search.json()["items"][0]["note_id"] == note["id"]
        assert search.json()["items"][0]["preview"] == text[:300]
