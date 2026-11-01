import io
import wave
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.errors import ProcessingError
from app.integrations.knowledge import EnrichmentResult
from app.main import create_app
from app.models import Job, WorkerState, utc_now
from app.worker import Worker, worker_lock


@pytest.fixture
def system(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings=settings)) as client:
        worker = Worker(settings)
        try:
            yield client, worker
        finally:
            worker.close()


def png():
    output = io.BytesIO()
    Image.new("RGB", (80, 40), "white").save(output, format="PNG")
    return output.getvalue()


def audio(seconds=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000 * seconds)
    return output.getvalue()


def upload(client, content=None, kind="image"):
    response = client.post(
        "/api/v1/notes/import",
        data={"title": "Материал", "source_type": kind},
        files={
            "file": (
                "../../original.bin",
                png() if content is None else content,
                "application/octet-stream",
            )
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def read(client, note):
    response = client.get("/api/v1/notes/" + note["id"])
    assert response.status_code == 200, response.text
    return response.json()


def text_note(client):
    return client.post(
        "/api/v1/notes", json={"source_type": "text", "title": "Title", "text": "Original"}
    ).json()


class Adapter:
    def __init__(self):
        self.deleted = []

    def upsert_note(self, document):
        return EnrichmentResult(
            note_id=document.note_id,
            content_version=document.content_version,
            summary="Кратко",
            tags=["tag"],
            entities=[{"name": "AES", "type": "algorithm"}],
        )

    def delete_note(self, note_id):
        self.deleted.append(note_id)


def test_image_import_download_and_cleanup(system, monkeypatch):
    client, worker = system
    monkeypatch.setattr("app.processing.extract_image", lambda *args: "  Привет\r\nМир  ")
    note = upload(client)
    assert note["ingestion_status"] == "queued"
    assert note["extracted_text"] is None
    assert note["content_version"] == 0
    assert note["attachments"][0]["original_name"] == "original.bin"
    url = note["attachments"][0]["download_url"]
    downloaded = client.get(url)
    assert downloaded.content == png()
    assert "attachment;" in downloaded.headers["content-disposition"]
    assert worker.run_once()
    result = read(client, note)
    assert result["ingestion_status"] == "ready"
    assert result["extracted_text"] == "Привет\nМир"
    assert result["content_version"] == 1
    assert result["source"]["metadata"] == {"width": 80, "height": 40}
    assert result["attachments"][0]["mime_type"] == "image/png"
    assert client.get("/api/v1/notes?source_type=image&ingestion_status=ready").json()["total"] == 1
    assert client.get("/api/v1/notes?source_type=audio").json()["total"] == 0
    assert client.delete("/api/v1/notes/" + note["id"]).status_code == 204
    assert client.get(url).status_code == 404
    assert worker.run_once()
    assert client.get("/api/v1/notes/" + note["id"] + "/jobs").status_code == 404
    assert not list((worker.settings.data_dir / "uploads").glob("*/original"))


def test_audio_validation_and_metadata(system, monkeypatch):
    client, worker = system
    monkeypatch.setattr("app.processing.extract_audio", lambda *args: ("Речь", {"language": "ru"}))
    note = upload(client, audio(), "audio")
    worker.run_once()
    result = read(client, note)
    assert result["ingestion_status"] == "ready"
    assert result["source"]["metadata"]["duration_seconds"] == 1
    assert result["source"]["metadata"]["language"] == "ru"
    assert result["attachments"][0]["mime_type"] == "audio/wav"


def test_audio_duration_limit(system):
    client, worker = system
    worker.settings.audio_max_seconds = 1
    note = upload(client, audio(2), "audio")
    worker.run_once()
    assert read(client, note)["ingestion_error"]["code"] == "audio_too_long"


def test_invalid_content_and_retry(system, monkeypatch):
    client, worker = system
    note = upload(client, b"not an image")
    worker.run_once()
    result = read(client, note)
    assert result["ingestion_status"] == "failed"
    assert result["ingestion_error"]["code"] == "invalid_image"
    retry_url = "/api/v1/notes/" + note["id"] + "/retry"
    assert client.post(retry_url, json={"stage": "extract"}).status_code == 202
    assert client.post(retry_url, json={"stage": "extract"}).status_code == 409
    worker.run_once()
    jobs = client.get("/api/v1/notes/" + note["id"] + "/jobs").json()
    assert len(jobs) == 2
    assert all(job["status"] == "failed" for job in jobs)


def test_empty_ocr_and_missing_engine_are_visible(system, monkeypatch):
    client, worker = system
    note = upload(client)
    monkeypatch.setattr("app.processing.extract_image", lambda *args: "  ")
    worker.run_once()
    assert read(client, note)["ingestion_error"]["code"] == "empty_extracted_text"
    client.post("/api/v1/notes/" + note["id"] + "/retry", json={"stage": "extract"})

    def unavailable(*args):
        raise ProcessingError("ocr_unavailable", "OCR unavailable")

    monkeypatch.setattr("app.processing.extract_image", unavailable)
    worker.run_once()
    assert read(client, note)["ingestion_error"]["code"] == "ocr_unavailable"


def test_upload_size_empty_and_unknown_type(system):
    client, worker = system
    worker.settings.image_max_bytes = 10
    for content, expected in [(b"12345678901", 413), (b"", 415)]:
        response = client.post(
            "/api/v1/notes/import",
            data={"title": "x", "source_type": "image"},
            files={"file": ("x", content)},
        )
        assert response.status_code == expected
    assert client.get("/api/v1/notes").json()["total"] == 0
    assert not list((worker.settings.data_dir / "uploads").glob("*/original"))
    assert (
        client.post(
            "/api/v1/notes/import",
            data={"title": "x", "source_type": "pdf"},
            files={"file": ("x", b"x")},
        ).status_code
        == 422
    )


def test_web_snapshot_survives_failed_extraction(system, monkeypatch):
    client, worker = system
    calls = []
    html = b"<html><body><article><p>A useful article about local notes and information security.</p></article></body></html>"

    def download(url, settings):
        calls.append(url)
        return html, url

    monkeypatch.setattr("app.processing.download_html", download)
    monkeypatch.setattr("app.processing.extract_html", lambda value: "")
    response = client.post(
        "/api/v1/notes", json={"source_type": "url", "title": "Web", "url": "https://example.com/"}
    )
    assert response.status_code == 202
    note = response.json()
    worker.run_once()
    failed = read(client, note)
    assert failed["ingestion_status"] == "failed"
    download_response = client.get(failed["attachments"][0]["download_url"])
    assert download_response.content == html
    assert download_response.headers["content-type"] == "application/octet-stream"
    monkeypatch.setattr("app.processing.extract_html", lambda value: "Extracted")
    client.post("/api/v1/notes/" + note["id"] + "/retry", json={"stage": "extract"})
    worker.run_once()
    assert read(client, note)["extracted_text"] == "Extracted"
    assert len(calls) == 1


def test_private_url_is_rejected_by_worker(system):
    client, worker = system
    note = client.post(
        "/api/v1/notes", json={"source_type": "url", "title": "Private", "url": "http://127.0.0.1/"}
    ).json()
    worker.run_once()
    assert read(client, note)["ingestion_error"]["code"] == "url_not_allowed"


def test_edit_while_extracting_and_delete_before_processing(system):
    client, worker = system
    note = upload(client)
    path = "/api/v1/notes/" + note["id"]
    assert client.patch(path, json={"expected_version": 1, "title": "New"}).status_code == 409
    assert client.delete(path).status_code == 204
    assert worker.run_once()
    assert client.get(path).status_code == 404
    assert worker.run_once() is None


def test_enrichment_edit_and_reindex(system):
    client, worker = system
    assert client.post("/api/v1/notes/reindex").status_code == 409
    note = text_note(client)
    worker.settings.knowledge_adapter = "test:factory"
    client.app.state.settings.knowledge_adapter = "test:factory"
    assert client.post("/api/v1/notes/reindex").json() == {"queued": 1}
    assert client.post("/api/v1/notes/reindex").json() == {"queued": 0}
    adapter = Adapter()
    worker.run_once(adapter)
    assert read(client, note)["summary"] == "Кратко"
    path = "/api/v1/notes/" + note["id"]
    edited = client.patch(path, json={"expected_version": 1, "extracted_text": "Edited"}).json()
    assert edited["summary"] is None
    assert edited["enrichment_status"] == "queued"
    assert edited["source"]["original_text"] == "Original"
    worker.run_once(adapter)
    assert read(client, note)["enrichment_version"] == 2
    client.delete(path)
    worker.run_once(adapter)
    assert adapter.deleted == [note["id"]]


@pytest.mark.parametrize("action", ["edit", "delete"])
def test_stale_enrichment_is_discarded(system, action):
    client, worker = system
    worker.settings.knowledge_adapter = "test:factory"
    client.app.state.settings.knowledge_adapter = "test:factory"
    note = text_note(client)
    path = "/api/v1/notes/" + note["id"]

    class ChangingAdapter(Adapter):
        def upsert_note(self, document):
            if action == "edit":
                assert (
                    client.patch(path, json={"expected_version": 1, "title": "Changed"}).status_code
                    == 200
                )
            else:
                assert client.delete(path).status_code == 204
            return super().upsert_note(document)

    adapter = ChangingAdapter()
    worker.run_once(adapter)
    if action == "edit":
        updated = read(client, note)
        assert updated["content_version"] == 2
        assert updated["summary"] is None
        assert updated["enrichment_status"] == "queued"
    else:
        assert client.get(path).status_code == 404
        worker.run_once(adapter)
        assert adapter.deleted == [note["id"]]


def test_cleanup_failure_can_be_retried(system):
    client, worker = system
    worker.settings.knowledge_adapter = "test:factory"
    client.app.state.settings.knowledge_adapter = "test:factory"
    note = text_note(client)
    adapter = Adapter()
    worker.run_once(adapter)

    class BrokenAdapter(Adapter):
        def delete_note(self, note_id):
            raise RuntimeError("Connection lost")

    path = "/api/v1/notes/" + note["id"]
    client.delete(path)
    worker.run_once(BrokenAdapter())
    assert client.get(path + "/jobs").json()[0]["status"] == "failed"
    assert client.delete(path).status_code == 204
    worker.run_once(adapter)
    assert client.get(path + "/jobs").status_code == 404


def test_worker_recovers_claimed_job(system, monkeypatch):
    client, worker = system
    monkeypatch.setattr("app.processing.extract_image", lambda *args: "Recovered")
    note = upload(client)
    job_id = worker.claim()
    assert read(client, note)["ingestion_status"] == "processing"
    worker.recover()
    assert read(client, note)["ingestion_status"] == "queued"
    worker.run_once()
    assert read(client, note)["extracted_text"] == "Recovered"
    with worker.factory() as session:
        assert session.get(Job, job_id).attempts == 2


def test_worker_lock_prevents_second_worker(system):
    _, worker = system
    with worker_lock(worker.settings):
        with pytest.raises(RuntimeError, match="already running"):
            with worker_lock(worker.settings):
                pass


def test_readiness_and_cors(system):
    client, worker = system
    assert client.get("/api/v1/ready").status_code == 503
    worker.heartbeat()
    assert client.get("/api/v1/ready").status_code == 200
    with worker.factory() as session:
        state = session.get(WorkerState, 1)
        state.heartbeat_at = utc_now() - timedelta(seconds=20)
        session.commit()
    assert client.get("/api/v1/ready").status_code == 503
    response = client.options(
        "/api/v1/notes",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert client.get("/api/v1/capabilities").json()["max_audio_seconds"] == 600


def test_body_limit_checks_actual_stream(tmp_path):
    settings = Settings(
        data_dir=tmp_path, image_max_bytes=10, audio_max_bytes=10, text_max_chars=10
    )
    with TestClient(create_app(settings=settings)) as client:
        response = client.post(
            "/api/v1/notes",
            content=iter([b"x" * 1_100_000]),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "file_too_large"


def test_openapi_documents_all_routes_and_real_error_shape(system):
    client, _ = system
    spec = client.get("/openapi.json").json()
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema({"$defs": spec["components"]["schemas"]})
    for path, operations in spec["paths"].items():
        for operation in operations.values():
            assert operation["summary"] and operation["description"], path
            if "422" in operation["responses"]:
                assert operation["responses"]["422"]["content"]["application/json"]["schema"][
                    "$ref"
                ].endswith("/ErrorResponse")
    assert "202" in spec["paths"]["/api/v1/notes"]["post"]["responses"]
    assert "content" not in spec["paths"]["/api/v1/notes/{note_id}"]["delete"]["responses"]["204"]


def test_wrong_adapter_result_is_rejected(system):
    client, worker = system
    worker.settings.knowledge_adapter = "test:factory"
    client.app.state.settings.knowledge_adapter = "test:factory"
    note = text_note(client)

    class WrongAdapter(Adapter):
        def upsert_note(self, document):
            result = super().upsert_note(document)
            return result.model_copy(update={"content_version": 999})

    worker.run_once(WrongAdapter())
    result = read(client, note)
    assert result["summary"] is None
    assert result["enrichment_error"]["code"] == "invalid_enrichment_result"
    assert result["ingestion_status"] == "ready"


def test_configured_text_limit(system):
    client, worker = system
    client.app.state.settings.text_max_chars = 5
    response = client.post(
        "/api/v1/notes", json={"source_type": "text", "title": "x", "text": "123456"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "text_too_large"
