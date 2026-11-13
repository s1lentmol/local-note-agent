"""Deterministic offline adapters. Explicit fixtures, no network or real models."""

from collections import deque

from .contracts import ModelClientError


class FakeTextGenerator:
    def __init__(self, responses=(), structured=()):
        self.responses, self.structured = deque(responses), deque(structured)
        self.requests = []

    def chat(self, messages, tools=()):
        self.requests.append((messages, tools))
        if not self.responses:
            raise ModelClientError("fake_responses_exhausted")
        return self.responses.popleft()

    def json(self, messages, schema):
        self.requests.append((messages, schema))
        if not self.structured:
            raise ModelClientError("fake_responses_exhausted")
        try:
            return schema.model_validate_json(self.structured.popleft(), strict=True)
        except ValueError:
            raise ModelClientError("invalid_structured_result") from None


class FakeEmbedder:
    """Fixture keyed by (document/query, exact text), distinct from real index identity."""

    def __init__(self, fixtures, dimensions):
        self.fixtures, self.dimensions = fixtures, dimensions
        self.identity = "fake-fixtures-v1"

    def documents(self, texts):
        return [list(self.fixtures["document", t]) for t in texts]

    def queries(self, texts):
        return [list(self.fixtures["query", t]) for t in texts]


class FakeTranscriber:
    def __init__(self, result):
        self.result = result

    def transcribe(self, audio):
        return self.result.model_copy()
