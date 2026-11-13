from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from app.dto import (
    BatchImportFields,
    ChatResponse,
    CollectionNotesAdd,
    GraphEdge,
    InternalChunk,
    NoteCreate,
    NoteDetail,
    NotePatch,
    Page,
    RetryRequest,
    SearchRequest,
)


def test_discriminated_create():
    adapter = TypeAdapter(NoteCreate)
    assert adapter.validate_python({"source_type": "text", "text": " привет "}).text == "привет"
    assert str(
        adapter.validate_python({"source_type": "url", "url": "https://example.org"}).url
    ).startswith("https:")
    for data in [
        {"source_type": "text", "url": "https://example.org"},
        {"source_type": "url", "url": "file:///tmp/private"},
        {"source_type": "text", "text": " "},
        {"source_type": "audio", "text": "a"},
        {"source_type": "text", "text": "a", "title": "x" * 201},
    ]:
        with pytest.raises(ValidationError):
            adapter.validate_python(data)


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"title": None},
        {"title": " "},
        {"extracted_text": None},
        {"title": "ok", "expected_version": 1},
    ],
)
def test_patch_rejects_invalid(data):
    with pytest.raises(ValidationError):
        NotePatch(**data)


def test_patch_and_other_request_validation():
    assert NotePatch(title="name").model_dump(exclude_unset=True) == {"title": "name"}
    assert NotePatch(extracted_text="text").extracted_text == "text"
    for model, values in [
        (BatchImportFields, {"collection_id": uuid4(), "collection_title": "test"}),
        (SearchRequest, {"query": "x", "limit": 101}),
        (
            SearchRequest,
            {"query": "x", "date_from": "2026-01-02T00:00:00Z", "date_to": "2026-01-01T00:00:00Z"},
        ),
        (RetryRequest, {"stage": "cleanup"}),
        (CollectionNotesAdd, {"note_ids": []}),
        (Page, {"items": [], "total": 0, "offset": -1}),
        (GraphEdge, {"source": uuid4(), "target": uuid4(), "score": float("nan")}),
        (InternalChunk, {"text": "x", "order": 0, "embedding": [0.1, 0.2]}),
    ]:
        with pytest.raises(ValidationError):
            model(**values)


def test_note_and_chat_serialization():
    note = NoteDetail(
        id=uuid4(),
        title="Конспект",
        source={"type": "text", "original_text": "Текст"},
        extracted_text="Текст",
        ingestion_status="ready",
        processing_status="not_requested",
        created_at="2026-10-22T19:30:00Z",
        updated_at="2026-10-22T19:30:00Z",
    )
    payload = note.model_dump(mode="json")
    assert NoteDetail.model_validate_json(note.model_dump_json()) == note
    assert payload["source"] == {
        "type": "text",
        "original_text": "Текст",
        "original_url": None,
        "metadata": {},
    }
    assert payload["attachments"] == []
    assert payload["processing_error"] is None
    assert payload["created_at"].endswith("Z")
    with pytest.raises(ValidationError):
        NoteDetail(**{**payload, "created_at": "2026-01-01T00:00:00"})
    chat = ChatResponse(
        conversation_id=uuid4(), text="Сохранено", operations=[{"type": "save_note", "id": note.id}]
    )
    assert set(chat.model_dump()) == {"conversation_id", "text", "operations"}
    with pytest.raises(ValidationError):
        ChatResponse(**{**chat.model_dump(), "citations": []})
