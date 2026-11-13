from http.server import HTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from app.model_clients import ASRSettings, HTTPTranscriber
from app.model_clients.asr_service import handler_for
from app.model_clients.contracts import ModelClientError, Transcript
from app.model_clients.testing import FakeTranscriber


def test_real_http_boundary(tmp_path):
    server = HTTPServer(
        ("127.0.0.1", 0),
        handler_for(FakeTranscriber(Transcript(text="Полный текст", language="ru")), max_bytes=8),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    path = tmp_path / "voice.txt"
    path.write_bytes(b"ogg")
    try:
        client = HTTPTranscriber(ASRSettings(base_url=base))
        assert client.transcribe(path).text == "Полный текст"
        with urlopen(base + "/health") as response:
            assert response.status == 200
        with pytest.raises(HTTPError) as exc:
            urlopen(Request(base + "/transcribe", data=b"123456789"))
        assert exc.value.code == 413
        path.write_bytes(b"123456789")
        with pytest.raises(ModelClientError, match="http_413"):
            client.transcribe(path)
        with pytest.raises(HTTPError) as exc:
            urlopen(Request(base + "/missing", data=b"x"))
        assert exc.value.code == 404
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
