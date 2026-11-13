"""T13: complete process and guarded enrichment on isolated PostgreSQL/pgvector."""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.dto import NotePatch, TextNoteCreate
from app.enrichment import EnrichmentSettings
from app.indexing import ChunkSettings, ProcessHandler, handlers, publish_process
from app.job_runner.worker import HandlerBinding, Worker
from app.jobs_repository import JobsRepository, StaleJob
from app.model_clients.settings import EmbeddingSettings
from app.postgres import Database
from app.services.notes import NotesService
from app.services.processing import enqueue_process
from tests.enrichment_fixtures import VALID, GeneratorFactory
from tests.indexing_fixtures import FixtureEmbedder


@pytest.fixture
def system(db, database_url):
    with Database(database_url) as database:
        service, jobs = NotesService(database, processing_enabled=True), JobsRepository(database)
        note = service.create(
            TextNoteCreate(source_type="text", text="Mars colony plans", title="My title")
        )
        yield service, jobs, note


def handler(database_url, response=VALID, **kwargs):
    return ProcessHandler(
        database_url,
        EmbeddingSettings(),
        ChunkSettings(3, 1),
        FixtureEmbedder,
        EnrichmentSettings(enabled=True, **kwargs),
        GeneratorFactory(response),
    )


def run(jobs, compute):
    assert Worker(jobs, {"process": HandlerBinding(compute, publish_process)}).run_once()


def test_success_manual_data_and_repeat(system, db, database_url):
    service, jobs, note = system
    collection = uuid4()
    db.execute("UPDATE notes SET manual_tags='[\"mine\"]' WHERE id=%s", (note.id,))
    db.execute("INSERT INTO collections(id,title) VALUES (%s,'Mine')", (collection,))
    db.execute(
        "INSERT INTO collection_notes(collection_id,note_id) VALUES (%s,%s)", (collection, note.id)
    )
    for attempt in range(2):
        if attempt:
            with jobs.database.transaction() as conn:
                assert enqueue_process(conn, note.id)
        run(jobs, handler(database_url))
        actual = service.get(note.id)
        assert actual.processing_status == "ready"
        assert actual.summary == "Plans for Mars"
        assert actual.tags == ["space"] and actual.entities == ["Mars"]
        assert actual.category == "science" and actual.title == "My title"
        assert db.execute("SELECT count(*) FROM enrichments").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2
        assert db.execute("SELECT model,prompt_version FROM enrichments").fetchone() == (
            "qwen3:8b",
            "note-enrichment-v1",
        )
        assert db.execute("SELECT manual_tags FROM notes").fetchone()[0] == ["mine"]
        assert db.execute("SELECT collection_id FROM collection_notes").fetchone()[0] == collection


@pytest.mark.parametrize(
    "response", [None, "invalid json", "{}", VALID.replace('["space"]', "[3]")]
)
def test_failure_keeps_index_and_retry_replaces(system, db, database_url, response):
    service, jobs, note = system
    run(jobs, handler(database_url, response))
    failed = service.get(note.id)
    assert failed.processing_status == "failed"
    assert failed.processing_error.code == "handler_failed"
    assert failed.processing_error.message == "Job handler failed"
    assert failed.extracted_text == note.extracted_text
    assert failed.summary is None and failed.tags == []
    assert db.execute("SELECT count(*) FROM enrichments").fetchone()[0] == 0
    old = db.execute("SELECT id FROM chunks").fetchall()
    assert len(old) == 2
    job_id = db.execute("SELECT id FROM jobs").fetchone()[0]
    assert jobs.retry(job_id)
    run(jobs, handler(database_url))
    assert service.get(note.id).processing_status == "ready"
    assert db.execute("SELECT attempts FROM jobs").fetchone()[0] == 2
    assert len(db.execute("SELECT id FROM chunks").fetchall()) == 2
    assert not set(old) & set(db.execute("SELECT id FROM chunks").fetchall())
    assert db.execute("SELECT count(*) FROM enrichments").fetchone()[0] == 1


@pytest.mark.parametrize("mutation", ["cancel", "patch", "delete", "expire", "token"])
def test_mutation_during_generation_rejects_result(system, db, database_url, mutation):
    service, jobs, note = system
    job = jobs.claim(30)
    published = []

    class Context:
        def publish(self, result):
            jobs.publish(job, publish_process, result)
            published.append(result)

    def factory(settings):
        delegate = GeneratorFactory()(settings)

        class Generator:
            def json(self, messages, schema):
                assert len(published) == 1  # Index committed before generation.
                if mutation == "patch":
                    service.patch(note.id, NotePatch(extracted_text="new content"))
                elif mutation == "delete":
                    service.delete(note.id)
                elif mutation == "cancel":
                    db.execute(
                        "UPDATE jobs SET status='cancelled', lease_until=NULL, finished_at=clock_timestamp() WHERE id=%s",
                        (job.id,),
                    )
                elif mutation == "expire":
                    db.execute(
                        "UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                        (job.id,),
                    )
                else:
                    db.execute(
                        "UPDATE jobs SET lease_token=gen_random_uuid() WHERE id=%s", (job.id,)
                    )
                return delegate.json(messages, schema)

        return Generator()

    with pytest.raises(StaleJob):
        replace(handler(database_url), generator_factory=factory)(job, Context())
    assert db.execute("SELECT count(*) FROM enrichments").fetchone()[0] == 0
    assert db.execute("SELECT summary,tags FROM notes").fetchone() == (None, [])


@pytest.mark.parametrize("kwargs", [{"max_input_bytes": 1}, {"max_output_bytes": 1}])
def test_budget_failure_preserves_index(system, db, database_url, kwargs):
    service, jobs, note = system
    run(jobs, handler(database_url, **kwargs))
    assert service.get(note.id).processing_status == "failed"
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM enrichments").fetchone()[0] == 0


def test_disabled_does_not_configure_generator(system, db, database_url, monkeypatch):
    service, jobs, note = system
    monkeypatch.setenv("NOTE_AGENT_DATABASE_URL", database_url)
    monkeypatch.setenv("LLM_MAX_TOKENS", "invalid")
    monkeypatch.setenv("NOTE_AGENT_ENRICHMENT_ENABLED", "false")
    assert not handlers()["process"].compute.enrichment.enabled
    run(jobs, replace(handler(database_url), enrichment=EnrichmentSettings()))
    assert service.get(note.id).processing_status == "ready"
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM enrichments").fetchone()[0] == 0


@pytest.mark.parametrize("unavailable", [False, True])
def test_production_generator_json_mode(system, db, database_url, monkeypatch, unavailable):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    service, jobs, note = system
    requests = []

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] > 0
            self.send_response(503 if unavailable else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": VALID},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ).encode()
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        monkeypatch.setenv("NOTE_AGENT_DATABASE_URL", database_url)
        monkeypatch.setenv("NOTE_AGENT_ENRICHMENT_ENABLED", "true")
        monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
        monkeypatch.setenv("LLM_MODEL", "fixture-generator")
        monkeypatch.setenv("LLM_MAX_TOKENS", "3")  # T13 supplies its own response budget.
        binding = handlers()["process"]
        compute = replace(binding.compute, embedder_factory=FixtureEmbedder)
        assert Worker(jobs, {"process": replace(binding, compute=compute)}).run_once()
        assert service.get(note.id).processing_status == ("failed" if unavailable else "ready")
        assert db.execute("SELECT count(*) FROM enrichments").fetchone()[0] == (
            0 if unavailable else 1
        )
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] > 0
        assert requests[0]["response_format"] == {"type": "json_object"}
        assert requests[0]["max_tokens"] == 1024
        assert requests[0]["model"] == "fixture-generator"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
