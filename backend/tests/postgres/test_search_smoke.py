"""Opt-in real T07 embeddings + SQL search on a synthetic disposable corpus."""

import os

import pytest

from app.dto import SearchRequest
from app.model_clients.embeddings import OllamaEmbedder
from app.model_clients.settings import EmbeddingSettings
from app.postgres import Database
from app.repositories.index import IndexRepository, SpaceProfile
from app.services.search import SearchService
from tests.postgres.test_search import seed


@pytest.mark.skipif(os.environ.get("NOTE_AGENT_SEARCH_SMOKE") != "1", reason="real endpoint opt-in")
def test_search_real_endpoint(db, database_url):
    embedding = EmbeddingSettings.from_env()
    embedder = OllamaEmbedder(embedding)
    profile = SpaceProfile.from_settings(embedding)
    corpus = [
        ("Криптография", "Симметричное шифрование защищает данные общим секретным ключом."),
        ("Database", "Database transactions use rollback to undo uncommitted changes."),
        ("Cooking", "Bake bread with flour, water and yeast in a hot oven."),
    ]
    vectors = embedder.documents([f"{title}\n\n{text}" for title, text in corpus])
    notes = []
    for (title, text), vector in zip(corpus, vectors, strict=True):
        note = seed(db, title, text, axis=0)
        notes.append(note)
        db.execute("UPDATE chunks SET embedding=%s::vector WHERE note_id=%s", (str(vector), note))
    with Database(database_url) as database:
        with database.connection() as conn:
            IndexRepository.check_profile(conn, profile, lock=False)
        search = SearchService(database, embedder=embedder, profile=profile)
        for query, expected in [
            ("симметричное шифрование", notes[0]),
            ("database transactions", notes[1]),
            ("Как защитить сообщение общим секретом?", notes[0]),
            ("How can I undo changes before saving them permanently?", notes[1]),
        ]:
            result = search.search(SearchRequest(query=query, limit=3))
            rank = [item.note_id for item in result.items].index(expected) + 1
            print(f"query={query!r} expected_rank={rank} scores={[i.score for i in result.items]}")
            assert rank == 1
