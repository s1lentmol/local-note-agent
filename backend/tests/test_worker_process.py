from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.worker import Worker, worker_lock


def test_timeout_stops_child_and_marks_job_failed(tmp_path, monkeypatch):
    (tmp_path / "slow_adapter.py").write_text("""
import time
class Adapter:
    def upsert_note(self, document):
        time.sleep(30)
    def delete_note(self, note_id):
        pass
def factory(settings):
    return Adapter()
""")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    settings = Settings(
        data_dir=tmp_path, knowledge_adapter="slow_adapter:factory", job_timeout_seconds=2
    )
    with TestClient(create_app(settings=settings)) as client:
        note = client.post(
            "/api/v1/notes", json={"source_type": "text", "title": "Slow", "text": "Text"}
        ).json()
        worker = Worker(settings)
        try:
            with worker_lock(settings) as lock:
                job_id = worker.claim()
                worker.execute_isolated(job_id, lock)
            result = client.get("/api/v1/notes/" + note["id"]).json()
            assert result["enrichment_status"] == "failed"
            assert result["enrichment_error"]["code"] == "processing_timeout"
            assert result["ingestion_status"] == "ready"
        finally:
            worker.close()


def test_isolated_worker_extracts_saved_html(tmp_path):
    import io

    from app.models import Attachment, Note
    from app.services.storage import Storage

    settings = Settings(data_dir=tmp_path, job_timeout_seconds=30)
    with TestClient(create_app(settings=settings)) as client:
        note = client.post(
            "/api/v1/notes",
            json={
                "source_type": "url",
                "title": "Article",
                "url": "https://example.com/",
            },
        ).json()
        worker = Worker(settings)
        try:
            with worker.factory() as session:
                saved = session.get(Note, note["id"])
                html = (
                    "<html><body><article><p>"
                    + "Local knowledge keeps original sources available. " * 20
                    + "</p></article></body></html>"
                ).encode()
                key, size = Storage(tmp_path).save(io.BytesIO(html), saved.source.id, 100_000)
                saved.source.attachments.append(
                    Attachment(
                        storage_key=key,
                        size_bytes=size,
                        original_name="page.html",
                        mime_type="text/html",
                    )
                )
                session.commit()
            with worker_lock(settings) as lock:
                worker.execute_isolated(worker.claim(), lock)
            result = client.get("/api/v1/notes/" + note["id"]).json()
            assert result["ingestion_status"] == "ready", result
            assert "Local knowledge keeps original sources available." in result["extracted_text"]
        finally:
            worker.close()
