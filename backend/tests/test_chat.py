import json
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api_app import create_app
from app.chat_settings import ChatSettings
from app.config import Settings
from app.dto import SearchItem, SearchResponse
from app.model_clients.contracts import Message, ModelClientError
from app.model_clients.testing import FakeTextGenerator
from app.services.chat import NO_EVIDENCE, TOOL_BYTES, ChatService, byte_size
from tests.chat_fixtures import answer, tools


def test_invalid_arguments_and_unavailable_tool_are_paired_results():
    search = Mock()
    notes = Mock()
    generator = FakeTextGenerator(
        [
            tools(
                ("search_notes", {"query": 1}),
                ("search_notes", {"query": "wrong scope", "collection_id": str(uuid4())}),
                ("get_note", {"note_id": "x"}),
                ("save_note", {}),
            ),
            answer("Я успешно сохранил заметку."),
        ]
    )
    chat = ChatService(None, notes, search, generator)
    turn = chat._generate(Message(role="user", content="Сохрани"), [], None)

    assert [m.role for m in turn] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "tool",
        "tool",
        "assistant",
    ]
    assert [m.tool_call_id for m in turn[2:-1]] == [call.id for call in turn[1].tool_calls]
    assert [json.loads(m.content)["error"] for m in turn[2:-1]] == [
        "invalid_arguments",
        "invalid_arguments",
        "invalid_arguments",
        "unknown_tool",
    ]
    assert turn[-1].content == NO_EVIDENCE
    search.search.assert_not_called()
    notes.get.assert_not_called()


def test_final_step_has_no_tools_and_over_budget_batch_is_not_executed():
    search = Mock()
    generator = FakeTextGenerator(
        [
            tools(("search_notes", {"query": "a"}), ("search_notes", {"query": "b"})),
            tools(("search_notes", {"query": "c"})),
        ]
    )
    chat = ChatService(None, Mock(), search, generator, ChatSettings(max_steps=2, max_tool_calls=1))
    with pytest.raises(ModelClientError, match="invalid_chat_tool_call"):
        chat._generate(Message(role="user", content="Вопрос"), [], None)
    assert len(generator.requests) == 2
    assert generator.requests[1][1] == ()
    search.search.assert_not_called()


def test_call_budget_counts_invalid_calls():
    generator = FakeTextGenerator([tools(("search_notes", {"query": ""})), answer()])
    chat = ChatService(None, Mock(), Mock(), generator, ChatSettings(max_tool_calls=1))
    chat._generate(Message(role="user", content="Вопрос"), [], None)
    assert generator.requests[1][1] == ()


def test_context_drops_complete_turn_and_clips_utf8_and_json():
    note_id = uuid4()
    generator = FakeTextGenerator(
        [
            tools(("search_notes", {"query": "topic"})),
            tools(("get_note", {"note_id": str(note_id)})),
            answer(),
        ]
    )
    search, notes = Mock(), Mock()
    search.search.return_value = SearchResponse(
        items=[SearchItem(note_id=note_id, title="Title", preview="Превью" * 150, score=1)]
    )
    text = 'Экранирование "\\\\" и юникод. ' * 1000
    notes.get.return_value = Mock(id=note_id, title="Title", extracted_text=text)
    settings = ChatSettings(context_tokens=3400, tool_result_bytes=330)
    chat = ChatService(None, notes, search, generator, settings)
    history = [[Message(role="user", content="Я" * 1600), Message(role="assistant", content="Да")]]
    turn = chat._generate(Message(role="user", content="Вопрос"), history, None)
    clipped_preview = json.loads(turn[2].content)["notes"][0]["preview"]
    assert 0 < len(clipped_preview) < 900
    result = turn[-2].content
    returned = json.loads(result)["text"]
    assert 0 < len(returned) < len(text)
    assert text.startswith(returned)
    assert len(result.encode()) <= settings.tool_result_bytes
    for messages, available_tools in generator.requests:
        assert not any(m.content == "Да" for m in messages)
        assert (
            sum(map(byte_size, messages)) + (TOOL_BYTES if available_tools else 0)
            <= chat.input_budget
        )


def test_oversize_answer_fails_instead_of_publishing_partial_text():
    generator = FakeTextGenerator([answer("Я" * 65)])
    chat = ChatService(None, Mock(), Mock(), generator, ChatSettings(max_response_bytes=128))
    with pytest.raises(ModelClientError, match="invalid_chat_answer"):
        chat._generate(Message(role="user", content="Вопрос"), [], None)


def test_chat_route_without_database():
    with TestClient(create_app(Settings())) as client:
        url = "/api/v1/chat/messages"
        response = client.post(url, json={"message": "Вопрос"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "database_unavailable"
