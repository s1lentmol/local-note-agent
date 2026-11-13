import math
import unicodedata

from .contracts import ModelClientError
from .http import HTTPTransport


class OllamaEmbedder:
    """Only the Ollama P02 protocol is supported. Identity checked per batch."""

    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport or HTTPTransport()

    @property
    def identity(self):
        return f"{self.settings.model}@sha256:{self.settings.digest}"

    @property
    def dimensions(self):
        return self.settings.dimensions

    def _embed(self, texts, query):
        if not texts:
            return []
        if any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError("Expected nonempty texts")
        normalized = [" ".join(unicodedata.normalize("NFC", t).split()) for t in texts]
        inputs = (
            [f"Instruct: {self.settings.query_instruction}\nQuery: {t}" for t in normalized]
            if query
            else normalized
        )
        tags = self.transport.request(self.settings, "/api/tags")
        try:
            if not any(
                m["name"] == self.settings.model and m["digest"] == self.settings.digest
                for m in tags["models"]
            ):
                raise ModelClientError("embedding_identity_mismatch")
        except (KeyError, TypeError):
            raise ModelClientError("invalid_embedding_identity") from None
        raw = self.transport.request(
            self.settings,
            "/api/embed",
            {
                "model": self.settings.model,
                "input": inputs,
                "truncate": False,
                "keep_alive": "10m",
                "options": {"num_ctx": self.settings.num_ctx},
            },
        )
        try:
            if raw["model"] != self.settings.model:
                raise ValueError()
            vectors = raw["embeddings"]
            if len(vectors) != len(texts):
                raise ValueError()
            result = []
            for vector in vectors:
                if len(vector) != self.dimensions or any(
                    type(x) not in (float, int) or not math.isfinite(x) for x in vector
                ):
                    raise ValueError()
                norm = math.hypot(*vector)
                if not math.isfinite(norm) or abs(norm - 1) > self.settings.norm_tolerance:
                    raise ValueError()
                result.append([x / norm for x in vector])
            return result
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ModelClientError("invalid_embeddings") from None

    def documents(self, texts):
        return self._embed(texts, False)

    def queries(self, texts):
        return self._embed(texts, True)
