"""Shared search application boundary for HTTP, Telegram and dialogue tools."""

import math
import os

from pydantic import BaseModel, Field

from app.dto import SearchRequest, SearchResponse
from app.model_clients.contracts import Embedder, ModelClientError
from app.model_clients.embeddings import OllamaEmbedder
from app.model_clients.settings import EmbeddingSettings
from app.repositories.index import SpaceProfile
from app.repositories.search import SearchRepository


class SearchSettings(BaseModel):
    dense_enabled: bool = True
    lexical_candidates: int = Field(default=100, ge=1, le=10000)
    dense_candidates: int = Field(default=100, ge=1, le=10000)
    rrf_k: int = Field(default=60, ge=1, le=10000)
    preview_chars: int = Field(default=300, ge=1, le=2000)

    @classmethod
    def from_env(cls):
        return cls(
            **{
                name: os.environ["NOTE_AGENT_SEARCH_" + name.upper()]
                for name in cls.model_fields
                if "NOTE_AGENT_SEARCH_" + name.upper() in os.environ
            }
        )


class SearchError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class SearchService:
    def __init__(
        self,
        database,
        settings=None,
        *,
        embedder: Embedder | None = None,
        profile: SpaceProfile | None = None,
    ):
        self.database = database
        self.settings = settings or SearchSettings.from_env()
        self.embedder = None
        self.profile = None
        if self.settings.dense_enabled:
            embedding = EmbeddingSettings.from_env()
            self.embedder = embedder if embedder is not None else OllamaEmbedder(embedding)
            self.profile = profile or SpaceProfile.from_settings(embedding)

    def search(self, request: SearchRequest) -> SearchResponse:
        # Validate even for non-HTTP callers. NUL cannot be passed as PostgreSQL text.
        request = SearchRequest.model_validate(request.model_dump())
        vector = None
        if self.settings.dense_enabled:
            try:
                self.profile.check_embedder(self.embedder)
                with self.database.connection() as conn:
                    SearchRepository.check_profile(conn, self.profile)
            except ValueError:
                raise SearchError("embedding_profile_mismatch") from None
            # Connection returned and no transaction spans model computation.
            try:
                vectors = self.embedder.queries([request.query])
                self.profile.check_embedder(self.embedder)
                if len(vectors) != 1:
                    raise ValueError("Invalid embedding count")
                vector = vectors[0]
                if (
                    len(vector) != self.profile.dimensions
                    or any(type(x) not in (float, int) or not math.isfinite(x) for x in vector)
                    or abs(math.hypot(*vector) - 1) > 0.01
                ):
                    raise ValueError("Invalid embedding vector")
            except (ModelClientError, ValueError, TypeError, OverflowError):
                raise SearchError("embedding_unavailable") from None
        with self.database.transaction() as conn:
            if vector is not None:
                try:
                    # Hold SHARE through retrieval: UPDATE/DELETE of the profile must wait.
                    SearchRepository.check_profile(conn, self.profile, lock=True)
                except ValueError:
                    raise SearchError("embedding_profile_mismatch") from None
            return SearchResponse(
                items=SearchRepository.retrieve(conn, request, self.settings, vector)
            )
