"""T17 graph publication and traversal against isolated PostgreSQL/pgvector."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api_app import create_app
from app.config import Settings
from app.dto import GraphQuery, NotePatch, TextNoteCreate
from app.enrichment import EnrichmentSettings
from app.graph_settings import GraphSettings
from app.indexing import ChunkSettings, ProcessHandler, handlers, publish_process
from app.job_runner.worker import HandlerBinding, Worker
from app.jobs_repository import JobsRepository, StaleJob
from app.model_clients.settings import EmbeddingSettings
from app.postgres import Database
from app.repositories.cleanup import CleanupRepository
from app.repositories.graph import CALCULATION_SOURCE
from app.repositories.index import IndexResult, SpaceProfile
from app.repositories.notes import NotesRepository
from app.services.graph import GraphService
from app.services.notes import NotesService
from app.services.processing import enqueue_process
from app.services.processing import main as enqueue_existing
from tests.enrichment_fixtures import GeneratorFactory
from tests.indexing_fixtures import FixtureEmbedder, unit_vector


@pytest.fixture
def system(db, database_url):
    with Database(database_url) as database:
        yield NotesService(database, processing_enabled=True), JobsRepository(database)


def create(service, title):
    return service.create(TextNoteCreate(source_type="text", text=title, title=title)).id


def index(*vectors):
    return IndexResult(
        SpaceProfile.from_settings(EmbeddingSettings()),
        ["chunk"] * len(vectors),
        list(vectors),
    )


def put(jobs, note_id, *vectors):
    job = jobs.claim(10)
    assert job.note_id == note_id
    jobs.publish(job, publish_process, index(*vectors), complete=True)


def links(db):
    return db.execute(
        "SELECT source_note_id, target_note_id, score, calculation_source FROM note_links"
        " ORDER BY source_note_id, target_note_id"
    ).fetchall()


def test_centroid_threshold_top_k_and_repeat(system, db, database_url, monkeypatch):
    notes, jobs = system
    axis_x, axis_y, axis_z = (unit_vector(i) for i in range(3))
    choices = []
    for title, vectors in [
        ("Смешанная", (axis_x, axis_y)),
        ("Близкая", (axis_x,)),
        ("Дальняя", (axis_z,)),
        ("Близнец", (axis_x,)),
    ]:
        note_id = create(notes, title)
        put(jobs, note_id, *vectors)
        choices.append(note_id)
    mixed, first, _, second = choices
    anchor = create(notes, "Центр")

    monkeypatch.setenv("NOTE_AGENT_DATABASE_URL", database_url)
    monkeypatch.setenv("NOTE_AGENT_GRAPH_TOP_K", "1")
    writer = handlers()["process"].writer
    job = jobs.claim(10)
    assert job.note_id == anchor
    jobs.publish(job, writer, index(axis_x, axis_x), complete=True)

    def incident():
        return [row for row in links(db) if anchor in row[:2]]

    winner = min(first, second)
    assert incident() == [(*sorted((anchor, winner)), 1.0, CALCULATION_SOURCE)]

    for vectors in ((axis_x,), (axis_x, axis_y), (axis_z, [-v for v in axis_z])):
        with jobs.database.transaction() as conn:
            assert enqueue_process(conn, anchor)
        put(jobs, anchor, *vectors)
        if vectors == (axis_x,):
            assert incident() == [
                (*sorted((anchor, neighbor)), 1.0, CALCULATION_SOURCE)
                for neighbor in sorted((first, second))
            ]
        elif vectors == (axis_x, axis_y):
            assert incident() == [(*sorted((anchor, mixed)), 1.0, CALCULATION_SOURCE)]
        else:
            assert incident() == []


@pytest.mark.parametrize("mutation", ["token", "lease", "lease_after_write", "profile"])
def test_rejected_publication_preserves_previous_graph(system, db, mutation):
    notes, jobs = system
    a = create(notes, "A")
    put(jobs, a, unit_vector())
    b = create(notes, "B")
    put(jobs, b, unit_vector())
    before_links = links(db)
    before_chunks = db.execute("SELECT id FROM chunks ORDER BY id").fetchall()
    assert before_links
    with jobs.database.transaction() as conn:
        assert enqueue_process(conn, b)
    job = jobs.claim(10)
    payload = index(unit_vector(1))
    if mutation == "token":
        db.execute("UPDATE jobs SET lease_token=gen_random_uuid() WHERE id=%s", (job.id,))
    elif mutation == "lease":
        db.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (job.id,))
    elif mutation == "profile":
        payload = replace(payload, profile=replace(payload.profile, model="incompatible"))

    def writer(conn, job, result):
        publish_process(conn, job, result)
        if mutation == "lease_after_write":
            assert conn.execute("SELECT count(*) AS count FROM note_links").fetchone()["count"] == 0
            conn.execute(
                "SELECT pg_sleep(greatest(0, extract(epoch FROM %s - clock_timestamp())) + 0.01)",
                (
                    conn.execute("SELECT lease_until FROM jobs WHERE id=%s", (job.id,)).fetchone()[
                        "lease_until"
                    ],
                ),
            )

    if mutation == "lease_after_write":
        db.execute("UPDATE jobs SET lease_until=now()+interval '1 second' WHERE id=%s", (job.id,))
    with pytest.raises(ValueError if mutation == "profile" else StaleJob):
        jobs.publish(job, writer, payload, complete=True)
    assert links(db) == before_links
    assert db.execute("SELECT id FROM chunks ORDER BY id").fetchall() == before_chunks


def test_collection_limit_and_lifecycle(system, db):
    notes, jobs = system
    ids = sorted(create(notes, f"Запись {i}") for i in range(4))
    for _ in ids:
        job = jobs.claim(10)
        jobs.publish(job, publish_process, index(unit_vector()), complete=True)
    low, anchor, high, outside = ids
    collection = db.execute(
        "INSERT INTO collections(title) VALUES ('Тест') RETURNING id"
    ).fetchone()[0]
    with db.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO collection_notes(collection_id,note_id) VALUES (%s,%s)",
            [(collection, note_id) for note_id in (low, anchor, high)],
        )
    graph = GraphService(jobs.database)
    query = GraphQuery(note_id=anchor, collection_id=collection, limit=2)
    initial = graph.get(query)
    assert initial == graph.get(query)
    assert [n.id for n in initial.nodes] == [anchor, low]
    assert [(e.source, e.target) for e in initial.edges] == [(low, anchor)]
    general = graph.get(GraphQuery(collection_id=collection, limit=3))
    assert [n.id for n in general.nodes] == [low, anchor, high]
    assert len(general.edges) == 3
    assert graph.get(GraphQuery(note_id=outside, collection_id=collection)).nodes == []
    assert graph.get(GraphQuery(collection_id=uuid4())).nodes == []
    assert [n.id for n in graph.get(GraphQuery(note_id=anchor, limit=1)).nodes] == [anchor]

    notes.patch(anchor, NotePatch(extracted_text="Правка"))
    assert all(anchor not in row[:2] for row in links(db))
    assert graph.get(query).edges == []
    put(jobs, anchor, unit_vector())
    assert graph.get(query) == initial
    notes.delete(anchor)
    assert all(anchor not in (e.source, e.target) for e in graph.get(GraphQuery()).edges)
    assert graph.get(query).nodes == []
    job = jobs.claim(10)
    assert job.kind == "cleanup"
    jobs.publish(job, CleanupRepository.publish, None, complete=True)
    assert all(anchor not in row[:2] for row in links(db))


def test_general_graph_keeps_strongest_edge_with_small_limit(system):
    notes, jobs = system
    ids = []
    for title, axis in (("A", 0), ("B", 1), ("C", 0), ("D", 1)):
        note_id = create(notes, title)
        put(jobs, note_id, unit_vector(axis))
        ids.append(note_id)
    graph = GraphService(jobs.database).get(GraphQuery(limit=2))
    expected = min(sorted(ids[::2]), sorted(ids[1::2]))
    assert [node.id for node in graph.nodes] == expected
    assert [(edge.source, edge.target) for edge in graph.edges] == [tuple(expected)]


def test_neighbor_patch_in_progress_does_not_publish_obsolete_edge(system, db, monkeypatch):
    notes, jobs = system
    fallback = create(notes, "Запасная")
    put(jobs, fallback, [0.8, 0.6] + [0.0] * 1022)
    neighbor = create(notes, "Сосед")
    put(jobs, neighbor, unit_vector())
    anchor = create(notes, "Центр")
    job = jobs.claim(10)
    assert job.note_id == anchor
    locked, resume = Event(), Event()
    clear_derived = NotesRepository.clear_derived

    def pause_patch(repo, note_id):
        locked.set()
        assert resume.wait(5)
        clear_derived(repo, note_id)

    monkeypatch.setattr(NotesRepository, "clear_derived", pause_patch)
    with ThreadPoolExecutor(1) as executor:
        patch = executor.submit(notes.patch, neighbor, NotePatch(extracted_text="Новая версия"))
        try:
            assert locked.wait(3)
            jobs.publish(
                job,
                lambda conn, job, payload: publish_process(
                    conn, job, payload, graph=GraphSettings(top_k=1)
                ),
                index(unit_vector()),
                complete=True,
            )
            assert [row[:2] for row in links(db) if anchor in row[:2]] == [
                tuple(sorted((anchor, fallback)))
            ]
        finally:
            resume.set()
        patch.result(timeout=3)
    assert all(neighbor not in row[:2] for row in links(db))


def test_failed_enrichment_keeps_graph(system, db, database_url):
    notes, jobs = system
    neighbor = create(notes, "Mars neighbor")
    put(jobs, neighbor, unit_vector())
    current = create(notes, "Mars current")
    compute = ProcessHandler(
        database_url,
        EmbeddingSettings(),
        ChunkSettings(3, 1),
        FixtureEmbedder,
        EnrichmentSettings(enabled=True),
        GeneratorFactory("invalid JSON"),
    )
    assert Worker(jobs, {"process": HandlerBinding(compute, publish_process)}).run_once()
    assert notes.get(current).processing_status == "failed"
    assert db.execute("SELECT count(*) FROM chunks WHERE note_id=%s", (current,)).fetchone()[0] > 0
    assert links(db) == [(*sorted((current, neighbor)), 1.0, CALCULATION_SOURCE)]


def test_production_graph_for_existing_corpus(system, db, database_url, monkeypatch, capsys):
    notes, jobs = system
    a = create(notes, "Mars A")
    put(jobs, a, unit_vector())
    b = create(notes, "Mars B")
    put(jobs, b, unit_vector())
    # Simulate a corpus indexed before T17, then run the documented scheduler.
    db.execute("DELETE FROM note_links")
    monkeypatch.setenv("NOTE_AGENT_DATABASE_URL", database_url)
    monkeypatch.setattr("sys.argv", ["app.services.processing"])
    with TestClient(create_app(Settings(database_url=database_url))) as client:
        graph_url = "/api/v1/graph"
        assert client.get(graph_url).json() == {"nodes": [], "edges": []}
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
        enqueue_existing()
        assert capsys.readouterr().out.strip() == "Queued: 2"
        compute = ProcessHandler(
            database_url, EmbeddingSettings(), ChunkSettings(), FixtureEmbedder
        )
        worker = Worker(jobs, {"process": HandlerBinding(compute, publish_process)})
        assert worker.run_once() and worker.run_once()

        collection = client.post("/api/v1/collections", json={"title": "Mars"})
        assert collection.status_code == 201
        collection_id = collection.json()["id"]
        members_url = f"/api/v1/collections/{collection_id}/notes"
        assert client.post(members_url, json={"note_ids": [str(a)]}).status_code == 204
        params = {"note_id": str(a), "limit": 2, "collection_id": collection_id}
        response = client.get(graph_url, params=params)
        assert response.status_code == 200, response.text
        assert [n["id"] for n in response.json()["nodes"]] == [str(a)]
        assert response.json()["edges"] == []

        assert client.post(members_url, json={"note_ids": [str(b)]}).status_code == 204
        response = client.get(graph_url, params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert [n["id"] for n in body["nodes"]] == [str(a), str(b)]
        assert len(body["edges"]) == 1
        for node in body["nodes"]:
            detail = client.get(f"/api/v1/notes/{node['id']}")
            assert detail.status_code == 200
            assert detail.json()["title"] == node["title"]
        for params in ({"note_id": "bad"}, {"collection_id": "bad"}, {"limit": 0}, {"limit": 101}):
            assert client.get(graph_url, params=params).status_code == 422


def test_production_graph_without_database():
    with TestClient(create_app(Settings(database_url=""))) as client:
        response = client.get("/api/v1/graph")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
