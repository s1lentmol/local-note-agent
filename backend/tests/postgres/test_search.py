"""Search acceptance against isolated PostgreSQL/pgvector, never a user's corpus."""

from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api_app import create_app
from app.config import Settings
from app.dto import SearchRequest
from app.model_clients.settings import EmbeddingSettings
from app.postgres import Database
from app.repositories.index import SpaceProfile
from app.repositories.search import SearchRepository
from app.services.search import SearchError, SearchService, SearchSettings
from tests.indexing_fixtures import FixtureEmbedder, unit_vector


def seed(db, title, text, *, axis=None, copies=1):
    note = db.execute(
        """INSERT INTO notes(title,extracted_text,ingestion_status)
           VALUES (%s,%s,'ready') RETURNING id""",
        (title, text),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO sources(note_id,type,original_text) VALUES (%s,'text',%s)", (note, text)
    )
    if axis is not None:
        job = db.execute(
            "INSERT INTO jobs(note_id,kind) VALUES (%s,'process') RETURNING id", (note,)
        ).fetchone()[0]
        for pos in range(copies):
            db.execute(
                """INSERT INTO chunks(note_id,job_id,position,text,embedding)
                   VALUES (%s,%s,%s,%s,%s::vector)""",
                (note, job, pos, text, str(unit_vector(axis))),
            )
    return note


def service(database, **kwargs):
    return SearchService(database, embedder=FixtureEmbedder(EmbeddingSettings()), **kwargs)


def ids(result):
    return [item.note_id for item in result.items]


def test_lexical_ru_en_live_updates_and_no_llm(db, database_url, monkeypatch):
    monkeypatch.setenv("NOTE_AGENT_SEARCH_DENSE_ENABLED", "false")
    monkeypatch.setenv("LLM_BASE_URL", "invalid")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "invalid")
    ru = seed(db, "Шифрование", "Симметричные ключи защищают данные")
    en = seed(db, "Database", "Transactions protect stored records")
    with TestClient(create_app(Settings(database_url=database_url))) as client:

        def query(text):
            response = client.post("/api/v1/search", json={"query": text})
            assert response.status_code == 200
            return response.json()["items"]

        assert query("симметричный ключ")[0]["note_id"] == str(ru)
        assert query("transaction record")[0]["note_id"] == str(en)
        assert set(query("Шифрование")[0]) == {"note_id", "title", "preview", "score"}
        assert query("несуществующееслово") == []
        assert query("!!!") == []
        db.execute("UPDATE notes SET extracted_text='Новая криптография' WHERE id=%s", (ru,))
        assert query("симметричный ключ") == []
        assert query("криптография")[0]["note_id"] == str(ru)
        db.execute("UPDATE notes SET deleted_at=now() WHERE id=%s", (ru,))
        assert query("криптография") == []


def test_semantic_fusion_note_dedup_and_limit(db, database_url):
    both = seed(db, "Mars", "Mars exploration", axis=0, copies=105)
    semantic = seed(db, "Planet", "A red world beyond Earth", axis=0)
    lexical = seed(db, "Vocabulary", "Mars is a word")
    seed(db, "Ocean", "Whales and fish", axis=1)
    with Database(database_url) as database:
        search = service(database, settings=SearchSettings(dense_candidates=2))
        result = search.search(SearchRequest(query="Mars"))
        assert ids(result)[0] == both
        assert set(ids(result)) == {both, semantic, lexical}
        assert len(ids(result)) == len(set(ids(result)))
        dense_rank = 1 if both < semantic else 2
        assert result.items[0].score == pytest.approx(1 / 61 + 1 / (60 + dense_rank))
        assert ids(search.search(SearchRequest(query="Mars", limit=1))) == [both]
        assert len(search.search(SearchRequest(query="Mars", limit=100)).items) == 3


@pytest.mark.parametrize("dense", [False, True])
def test_filters_before_candidates_and_deleted(db, database_url, dense):
    target = seed(db, "Mars", "Mars suitable", axis=0, copies=3)
    deleted = seed(db, "Mars", "Mars hidden", axis=0)
    db.execute("UPDATE notes SET deleted_at=now() WHERE id=%s", (deleted,))
    db.execute("UPDATE notes SET created_at='2026-01-02T12:00:00Z' WHERE id=%s", (target,))
    collection = db.execute(
        "INSERT INTO collections(title) VALUES ('test') RETURNING id"
    ).fetchone()[0]
    db.execute(
        "INSERT INTO collection_notes(collection_id,note_id) VALUES (%s,%s)", (collection, target)
    )
    # More excluded rows than the candidate budget; neither channel may consume them.
    for _ in range(105):
        seed(db, "Mars Mars Mars", "Mars Mars Mars", axis=0)
    with Database(database_url) as database:
        search = service(database, settings=SearchSettings(dense_enabled=dense))
        base = dict(
            query="Mars",
            source_type="text",
            collection_id=collection,
            date_from="2026-01-02T12:00:00Z",
            date_to="2026-01-02T12:00:00Z",
        )
        assert ids(search.search(SearchRequest(**base))) == [target]
        for changes in (
            {"source_type": "audio"},
            {"collection_id": uuid4()},
            {"date_from": "2026-01-02T13:00:00Z", "date_to": None},
            {"date_to": "2026-01-02T11:00:00Z", "date_from": None},
        ):
            assert search.search(SearchRequest(**(base | changes))).items == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "other"),
        ("revision", "0" * 64),
        ("dimensions", 100),
        ("metric", "dot"),
        ("normalization", "none"),
        ("query_instruction", "other"),
    ],
)
def test_full_profile(db, database_url, field, value):
    profile = replace(SpaceProfile.from_settings(EmbeddingSettings()), **{field: value})
    with Database(database_url) as database:
        search = service(database, profile=profile)
        with pytest.raises(SearchError, match="embedding_profile_mismatch"):
            search.search(SearchRequest(query="Mars"))
        assert search.embedder.requests == []


def test_profile_changes_during_model_call_and_no_open_transaction(db, database_url):
    with Database(database_url, max_size=1) as database:
        search = service(database)

        def queries(texts):
            # Pool size one: borrowing proves the service released its connection.
            with database.connection() as conn:
                assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            db.execute("UPDATE embedding_profiles SET query_instruction='changed'")
            return [unit_vector()]

        search.embedder.queries = queries
        with pytest.raises(SearchError, match="embedding_profile_mismatch"):
            search.search(SearchRequest(query="Mars"))


def test_profile_lock_covers_retrieval(db, database_url, monkeypatch):
    original = SearchRepository.retrieve

    def retrieve(conn, *args):
        with db.transaction():
            db.execute("SET LOCAL lock_timeout='100ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                db.execute("UPDATE embedding_profiles SET query_instruction='changed'")
        return original(conn, *args)

    monkeypatch.setattr(SearchRepository, "retrieve", retrieve)
    with Database(database_url) as database:
        assert service(database).search(SearchRequest(query="Mars")).items == []


def test_enabled_endpoint_failure_is_explicit(db, database_url, monkeypatch):
    seed(db, "Mars", "Mars lexical match")
    monkeypatch.setenv("NOTE_AGENT_SEARCH_DENSE_ENABLED", "true")
    # Real T07 transport to a closed ephemeral loopback port, no fake fallback.
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("EMBEDDING_BASE_URL", f"http://127.0.0.1:{port}")
    with TestClient(create_app(Settings(database_url=database_url))) as client:
        response = client.post("/api/v1/search", json={"query": "Mars"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "embedding_unavailable"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": ""},
        {"query": "  "},
        {"query": "a\x00b"},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 101},
        {"query": "x", "source_type": "bad"},
        {"query": "x", "collection_id": "bad"},
        {"query": "x", "date_from": "2026-01-01"},
        {"query": "x", "date_from": "2026-02-01T00:00:00Z", "date_to": "2026-01-01T00:00:00Z"},
        {"query": "x", "extra": True},
    ],
)
def test_search_validation(db, database_url, monkeypatch, body):
    monkeypatch.setenv("NOTE_AGENT_SEARCH_DENSE_ENABLED", "false")
    with TestClient(create_app(Settings(database_url=database_url))) as client:
        assert client.post("/api/v1/search", json=body).status_code == 422


@pytest.mark.parametrize(
    "vector", [[], [1.0], [0.0] * 1024, [float("nan")] * 1024, [float("inf")] * 1024]
)
def test_invalid_query_vectors(db, database_url, vector):
    with Database(database_url) as database:
        search = service(database)
        search.embedder.queries = lambda texts: [vector]
        with pytest.raises(SearchError, match="embedding_unavailable"):
            search.search(SearchRequest(query="Mars"))


def test_api_hybrid_and_profile_error(db, database_url, monkeypatch):
    monkeypatch.setenv("NOTE_AGENT_SEARCH_DENSE_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "invalid")
    expected = seed(db, "Planet", "A red world beyond Earth", axis=0)
    with TestClient(create_app(Settings(database_url=database_url))) as client:
        client.app.state.search.embedder = FixtureEmbedder(EmbeddingSettings())
        response = client.post("/api/v1/search", json={"query": "Mars"})
        assert response.status_code == 200
        assert response.json()["items"][0]["note_id"] == str(expected)
        db.execute("DELETE FROM chunks")
        db.execute("UPDATE embedding_profiles SET revision=%s", ("0" * 64,))
        response = client.post("/api/v1/search", json={"query": "Mars"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "embedding_profile_mismatch"


def test_search_migration_backfills_existing_notes(database_url, tmp_path):
    from migrate import MIGRATIONS, migrate

    (tmp_path / "001_initial.sql").write_bytes((MIGRATIONS / "001_initial.sql").read_bytes())
    migrate(database_url, tmp_path)
    with psycopg.connect(database_url, autocommit=True) as conn:
        expected = seed(conn, "Upgrade", "Existing transactions")
    assert migrate(database_url) == [2]
    with Database(database_url) as database:
        search = service(database, settings=SearchSettings(dense_enabled=False))
        assert ids(search.search(SearchRequest(query="transaction"))) == [expected]


def test_search_explain_analyze_thousand_notes(db, database_url):
    db.execute("""INSERT INTO notes(title,extracted_text,ingestion_status)
                  SELECT CASE WHEN i % 100 = 0 THEN 'Mars' ELSE 'Ocean' END,
                         'Synthetic search document number ' || i, 'ready'
                  FROM generate_series(1,1000) AS i""")
    db.execute("""INSERT INTO sources(note_id,type,original_text)
                  SELECT id,'text',extracted_text FROM notes""")
    db.execute("INSERT INTO jobs(note_id,kind) SELECT id,'process' FROM notes")
    db.execute(
        """INSERT INTO chunks(note_id,job_id,position,text,embedding)
                  SELECT n.id,j.id,0,n.extracted_text,%s::vector
                  FROM notes n JOIN jobs j ON j.note_id=n.id""",
        (str(unit_vector()),),
    )
    db.execute("ANALYZE notes")
    db.execute("ANALYZE chunks")
    db.execute("ANALYZE sources")

    class ExplainConnection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, statement, params):
            return self.conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, params)

    with Database(database_url) as database, database.transaction() as conn:
        SearchRepository.check_profile(
            conn, SpaceProfile.from_settings(EmbeddingSettings()), lock=True
        )
        row = SearchRepository.retrieve(
            ExplainConnection(conn), SearchRequest(query="Mars"), SearchSettings(), unit_vector()
        )[0]
        plan = row["QUERY PLAN"][0]
        assert plan["Plan"]["Actual Rows"] == 20
        assert plan["Execution Time"] > 0
        print(
            f"1000 notes/chunks EXPLAIN ANALYZE: planning={plan['Planning Time']} ms, "
            f"execution={plan['Execution Time']} ms"
        )
