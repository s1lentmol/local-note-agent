from app.model_clients.contracts import ModelClientError
from app.model_clients.testing import FakeEmbedder


def unit_vector(axis=0):
    return [float(i == axis) for i in range(1024)]


class FixtureEmbedder(FakeEmbedder):
    """Explicit test fixture in the configured space; no real model claims."""

    def __init__(self, settings, *, fail=False):
        self.settings = settings
        self.identity = f"{settings.model}@sha256:{settings.digest}"
        self.dimensions = settings.dimensions
        self.fail = fail
        self.requests = []

    def documents(self, texts):
        self.requests.append(texts)
        if self.fail:
            raise ModelClientError("synthetic_model_failure")
        return [unit_vector(0 if "Mars" in text else 1) for text in texts]

    def queries(self, texts):
        return self.documents(texts)


def failing_embedder(settings):
    return FixtureEmbedder(settings, fail=True)
