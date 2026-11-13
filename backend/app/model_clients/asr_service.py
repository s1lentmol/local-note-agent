"""Single-request native service. Run only on the host with existing P03 weights."""

import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .contracts import ModelClientError
from .native_asr import NativeWhisperTranscriber


def handler_for(transcriber, max_bytes=50 * 1024 * 1024, read_timeout=30):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # No request paths, bodies, file names or transcripts in logs.

        def setup(self):
            super().setup()
            self.connection.settimeout(read_timeout)

        def reply(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(
                200 if self.path == "/health" else 404,
                {"status": "ok"} if self.path == "/health" else {"error": "not_found"},
            )

        def do_POST(self):
            if self.path != "/transcribe":
                self.reply(404, {"error": "not_found"})
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError()
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= max_bytes:
                    self.reply(413, {"error": "invalid_audio_size"})
                    return
                # HTTPServer is deliberately sequential; no extra queue or workers.
                with tempfile.TemporaryDirectory(prefix="asr-http-") as temp:
                    audio = Path(temp) / "input"
                    remaining = size
                    with audio.open("wb") as target:
                        while remaining:
                            block = self.rfile.read(min(65536, remaining))
                            if not block:
                                raise ValueError()
                            target.write(block)
                            remaining -= len(block)
                    result = transcriber.transcribe(audio)
                self.reply(200, result.model_dump())
            except ModelClientError as exc:
                self.reply(422, {"error": exc.code})
            except (ValueError, OSError):
                self.reply(400, {"error": "invalid_request"})

    return Handler


def main():
    transcriber = NativeWhisperTranscriber(
        Path(os.environ["ASR_RUNTIME"]),
        Path(os.environ["ASR_MODEL"]),
        timeout=float(os.environ.get("ASR_RUNTIME_TIMEOUT", "600")),
        max_seconds=float(os.environ.get("ASR_MAX_SECONDS", "1800")),
    )
    server = HTTPServer(
        (os.environ.get("ASR_BIND", "127.0.0.1"), int(os.environ.get("ASR_PORT", "18082"))),
        handler_for(transcriber, int(os.environ.get("ASR_MAX_BYTES", str(50 * 1024 * 1024)))),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
