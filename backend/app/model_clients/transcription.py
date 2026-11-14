from pydantic import ValidationError

from .contracts import ModelClientError, Transcript
from .http import HTTPTransport


class HTTPTranscriber:
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport or HTTPTransport()

    def transcribe(self, audio):
        with audio.open("rb") as source:
            data = source.read(self.settings.max_bytes + 1)
        if not data or len(data) > self.settings.max_bytes:
            raise ModelClientError("invalid_audio_size")
        raw = self.transport.request(self.settings, "/transcribe", audio=data)
        if isinstance(raw, dict) and isinstance(raw.get("text"), str) and not raw["text"].strip():
            raise ModelClientError("empty_asr_result")
        try:
            return Transcript.model_validate(raw)
        except ValidationError:
            raise ModelClientError("invalid_transcript") from None
