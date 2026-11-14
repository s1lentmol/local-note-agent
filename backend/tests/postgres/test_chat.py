import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from aiogram import Bot
from fastapi.testclient import TestClient

from app.api_app import create_app
from app.bot.handlers import create_dispatcher
from app.chat_settings import ChatSettings
from app.config import Settings
from app.dto import ChatRequest
from app.model_clients.generation import OpenAITextGenerator
from app.model_clients.settings import EmbeddingSettings, LLMSettings
from app.model_clients.testing import FakeTextGenerator
from app.postgres import Database
from app.services.chat import NO_EVIDENCE, ChatError, ChatService, TelegramChatDelivery
from app.services.notes import NotesService
from app.services.search import SearchService, SearchSettings
from tests.chat_fixtures import answer, tools
from tests.indexing_fixtures import FixtureEmbedder
from tests.model_clients.test_clients import Transport, call, completion
from tests.postgres.test_search import seed
from tests.telegram_fixtures import RecordingSession, update

URL = "/api/v1/chat/messages"


def test_http_and_telegram_use_same_chat_service_with_full_native_cycle(
    db, database_url, monkeypatch
):
    note = seed(db, "Mars", "Mars test habitat uses recycled water.", axis=0)
    body = "The habitat uses recycled water."
    responses = (
        completion(
            None,
            [
                call('{"query":"Mars"}', "search_notes"),
                {**call('{"query":3}', "search_notes"), "id": "invalid_call"},
            ],
            "tool_calls",
        ),
        completion(None, [call(json.dumps({"note_id": str(note)}), "get_note")], "tool_calls"),
        completion(body),
        completion("It uses recycled water."),
    )
    transport = Transport(*responses)
    generator = OpenAITextGenerator(LLMSettings(), transport, validate_tool_arguments=False)
    embedder = FixtureEmbedder(EmbeddingSettings())
    original = ChatService.from_env

    def factory(database, notes, search):
        service = original(database, notes, search)
        service.generator = generator
        service.search = SearchService(database, embedder=embedder)
        return service

    monkeypatch.setattr(ChatService, "from_env", staticmethod(factory))
    app = create_app(Settings(database_url=database_url))
    with TestClient(app) as client:
        assert client.post(URL, json={"message": ""}).status_code == 422
        first = client.post(URL, json={"message": "How does the Mars habitat get water?"})
        assert first.status_code == 200, first.text
        response = first.json()
        assert response == {
            "conversation_id": response["conversation_id"],
            "text": body,
            "operations": [],
        }
        conversation_id = response["conversation_id"]
        followup = client.post(
            URL, json={"message": "What does it use?", "conversation_id": conversation_id}
        )
        assert followup.json()["text"] == "It uses recycled water."
        assert followup.json()["conversation_id"] == conversation_id

        saved = db.execute(
            """SELECT role, tool_calls, tool_call_id, text FROM messages
               ORDER BY created_at, id"""
        ).fetchall()
        assert [row[0] for row in saved] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "assistant",
            "tool",
            "assistant",
            "user",
            "assistant",
        ]
        assert saved[1][1][0]["name"] == "search_notes"
        assert saved[2][2] == saved[1][1][0]["id"]
        assert saved[3][2] == "invalid_call"
        assert json.loads(saved[3][3]) == {"error": "invalid_arguments"}
        assert saved[4][1][0]["name"] == "get_note"
        assert saved[5][2] == saved[4][1][0]["id"]
        assert json.loads(saved[5][3])["text"] == "Mars test habitat uses recycled water."
        followup_wire = transport.requests[-1][1]["messages"]
        assert [m["role"] for m in followup_wire] == ["system", *(row[0] for row in saved[:-1])]
        assert followup_wire[3]["tool_call_id"] == saved[2][2]
        assert followup_wire[6]["tool_call_id"] == saved[5][2]
        assert "untrusted data" in followup_wire[0]["content"]
        assert embedder.requests == [["Mars"]]

        service = app.state.chat
        service.generator = FakeTextGenerator(
            [tools(("search_notes", {"query": "Mars"})), answer(body)]
        )
        session = RecordingSession()

        async def scenario():
            async with Bot("123:test-synthetic", session=session) as bot:
                dispatcher = create_dispatcher(app.state.notes, service)
                payload = update(text="How does the Mars habitat get water?")
                await dispatcher.feed_update(bot, payload)
                await dispatcher.feed_update(bot, payload)

        asyncio.run(scenario())
        assert [reply.text for reply in session.sent] == [body, body]
        assert len(service.generator.requests) == 2
        assert db.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
        assert (
            db.execute(
                """SELECT count(*) FROM messages m JOIN conversations c ON c.id = m.conversation_id
               WHERE c.channel = 'telegram' AND m.role = 'user' AND m.delivery_id = '1'"""
            ).fetchone()[0]
            == 1
        )


def test_collection_scope_and_no_evidence(db, database_url):
    visible = seed(db, "Mars", "Mars habitat has water.")
    hidden = seed(db, "Mars", "Mars habitat has a secret.")
    collection = db.execute(
        "INSERT INTO collections(title) VALUES ('test') RETURNING id"
    ).fetchone()[0]
    db.execute(
        "INSERT INTO collection_notes(collection_id,note_id) VALUES (%s,%s)", (collection, visible)
    )
    generator = FakeTextGenerator(
        [
            tools(("search_notes", {"query": "Mars"})),
            tools(("get_note", {"note_id": str(hidden)})),
            answer("У поселения есть вода."),
            tools(("search_notes", {"query": "unicorn"})),
            answer("Единороги подтверждены."),
        ]
    )
    with Database(database_url) as database:
        chat = ChatService(
            database,
            NotesService(database),
            SearchService(database, SearchSettings(dense_enabled=False)),
            generator,
        )
        first = chat.reply(ChatRequest(message="Mars", collection_id=collection))
        assert first.text == "У поселения есть вода."
        second = chat.reply(
            ChatRequest(message="And unicorns?", conversation_id=first.conversation_id)
        )
        assert second.text == NO_EVIDENCE
        with pytest.raises(ChatError, match="другой подборки"):
            chat.reply(
                ChatRequest(
                    message="Mars", conversation_id=first.conversation_id, collection_id=uuid4()
                )
            )
        with pytest.raises(ChatError) as exc:
            chat.reply(ChatRequest(message="Mars", conversation_id=uuid4()))
        assert exc.value.status == 404
    assert [r["note_id"] for r in json.loads(generator.requests[1][0][-1].content)["notes"]] == [
        str(visible)
    ]
    assert json.loads(generator.requests[2][0][-1].content) == {
        "error": "note_not_in_search_results"
    }


def test_errors_leave_no_partial_turn_and_retry_can_succeed(db, database_url):
    seed(db, "Mars", "Mars test habitat.")
    with Database(database_url, max_size=1) as database:
        broken = SearchService(database, embedder=FixtureEmbedder(EmbeddingSettings(), fail=True))
        generator = FakeTextGenerator([tools(("search_notes", {"query": "Mars"}))])
        chat = ChatService(database, NotesService(database), broken, generator)
        delivery = TelegramChatDelivery(123, 42, 1)
        request = ChatRequest(message="Mars")
        with pytest.raises(ChatError, match="Модель"):
            chat.reply(request, delivery=delivery)
        with pytest.raises(ChatError, match="Модель"):
            chat.reply(request, delivery=delivery)
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0

        def generate(messages, tools=()):
            with database.connection() as conn:
                assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            return (
                answer("Найден Mars.") if not tools or messages[-1].role == "tool" else search_call
            )

        search_call = tools(("search_notes", {"query": "Mars"}))
        chat.search = SearchService(database, SearchSettings(dense_enabled=False))
        chat.generator.chat = generate
        saved = chat.reply(request, delivery=delivery)
        assert saved.text == "Найден Mars."
        assert chat.reply(request, delivery=delivery) == saved
        assert (
            chat.reply(request, delivery=TelegramChatDelivery(124, 42, 1)).conversation_id
            != saved.conversation_id
        )
        assert (
            chat.reply(request, delivery=TelegramChatDelivery(123, 43, 1)).conversation_id
            != saved.conversation_id
        )
    assert db.execute("SELECT count(*) FROM messages WHERE role = 'user'").fetchone()[0] == 3


def test_concurrent_redelivery_is_atomic_and_model_holds_no_row_lock(db, database_url):
    class SynchronizedGenerator:
        def chat(self, messages, tools=()):
            # The other call must be able to acquire the same conversation lock
            # and reach generation before this request returns from the model.
            barrier.wait(timeout=5)
            return answer()

    barrier = Barrier(2)
    with Database(database_url, max_size=1) as database, ThreadPoolExecutor(2) as pool:
        chat = ChatService(database, NotesService(database), None, SynchronizedGenerator())
        request = ChatRequest(message="Вопрос")
        delivery = TelegramChatDelivery(123, 42, 1)
        pending = [pool.submit(chat.reply, request, delivery=delivery) for _ in range(2)]
        first, second = [job.result(timeout=10) for job in pending]
        assert first == second
    assert db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2


def test_history_limit_excludes_partial_turn_and_web_cannot_open_telegram(db, database_url):
    generator = FakeTextGenerator([tools(("search_notes", {"query": "Mars"})), answer(), answer()])
    with Database(database_url) as database:
        chat = ChatService(
            database,
            NotesService(database),
            SearchService(database, SearchSettings(dense_enabled=False)),
            generator,
            ChatSettings(history_messages=3),
        )
        delivery = TelegramChatDelivery(123, 42, 1)
        first = chat.reply(ChatRequest(message="Mars"), delivery=delivery)
        second = chat.reply(
            ChatRequest(message="Follow-up"), delivery=TelegramChatDelivery(123, 42, 2)
        )
        assert first.conversation_id == second.conversation_id
        assert [m.role for m in generator.requests[-1][0]] == ["system", "user"]
        with pytest.raises(ChatError) as exc:
            chat.reply(ChatRequest(message="web", conversation_id=first.conversation_id))
        assert exc.value.status == 404


def test_concurrent_distinct_turns_can_be_retried(db, database_url):
    with Database(database_url) as database, ThreadPoolExecutor(2) as pool:
        chat = ChatService(database, NotesService(database), None, FakeTextGenerator([answer()]))
        start = chat.reply(ChatRequest(message="Начало"))
        barrier = Barrier(2)

        def generate(messages, tools=()):
            barrier.wait(timeout=5)
            return answer()

        chat.generator.chat = generate
        requests = [
            ChatRequest(message=message, conversation_id=start.conversation_id)
            for message in ("Первый вопрос", "Второй вопрос")
        ]
        pending = [pool.submit(chat.reply, request) for request in requests]
        failures = []
        for request, job in zip(requests, pending, strict=True):
            try:
                job.result(timeout=10)
            except ChatError as exc:
                assert (exc.status, exc.code) == (409, "conversation_busy")
                failures.append(request)

        assert len(failures) == 1
        chat.generator.chat = lambda *args: answer()
        chat.reply(failures[0])

    users = db.execute(
        "SELECT text FROM messages WHERE role='user' ORDER BY created_at, id"
    ).fetchall()
    assert users[0] == ("Начало",)
    assert {text for (text,) in users[1:]} == {request.message for request in requests}
