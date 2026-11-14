"""Opt-in RAG smoke with existing local Ollama weights and a synthetic corpus."""

import os

import pytest
from fastapi.testclient import TestClient

from app.api_app import create_app
from app.config import Settings
from app.model_clients.embeddings import OllamaEmbedder
from app.model_clients.settings import EmbeddingSettings
from tests.postgres.test_search import seed


@pytest.mark.skipif(os.environ.get("NOTE_AGENT_CHAT_SMOKE") != "1", reason="real endpoint opt-in")
def test_chat_real_endpoint(db, database_url):
    title = "Марсианская станция Селена"
    text = "Система охлаждения станции Селена использует раствор ацетата калия."
    note = seed(db, title, text, axis=0)
    embedder = OllamaEmbedder(EmbeddingSettings.from_env())
    vector = embedder.documents([f"{title}\n\n{text}"])[0]
    db.execute("UPDATE chunks SET embedding=%s::vector WHERE note_id=%s", (str(vector), note))

    with TestClient(create_app(Settings(database_url=database_url))) as client:
        response = client.post(
            "/api/v1/chat/messages",
            json={
                "message": "Что используется в системе охлаждения станции Селена? Найди в заметках."
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        print(f"answer={body['text']!r}")
        assert "ацетат" in body["text"].lower() and "кали" in body["text"].lower()
        assert body["operations"] == []
        assert (
            db.execute(
                """SELECT count(*) FROM messages
               WHERE conversation_id=%s AND role='tool' AND text LIKE %s""",
                (body["conversation_id"], f"%{note}%"),
            ).fetchone()[0]
            >= 1
        )
