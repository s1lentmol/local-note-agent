"""Sequential process computation; models and reads live in the spawned child."""

import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial

import psycopg
from psycopg.rows import dict_row

from app.cleanup import CleanupHandler
from app.config import Settings
from app.enrichment import EnrichmentResult, EnrichmentSettings, build_enrichment
from app.extraction import ExtractHandler, publish_extract
from app.graph_settings import GraphSettings
from app.job_runner.worker import HandlerBinding
from app.model_clients.contracts import Embedder, TextGenerator
from app.model_clients.embeddings import OllamaEmbedder
from app.model_clients.generation import OpenAITextGenerator
from app.model_clients.settings import EmbeddingSettings, LLMSettings
from app.repositories.cleanup import CleanupRepository
from app.repositories.enrichment import EnrichmentRepository
from app.repositories.index import IndexRepository, IndexResult, SpaceProfile


@dataclass(frozen=True)
class ChunkSettings:
    size: int = 600
    overlap: int = 80
    batch_size: int = 16

    def __post_init__(self):
        if self.size <= 0 or not 0 <= self.overlap < self.size or self.batch_size <= 0:
            raise ValueError("Invalid chunk size, overlap or batch size")

    @classmethod
    def from_env(cls):
        return cls(
            **{
                key: int(os.environ.get("NOTE_AGENT_CHUNK_" + key.upper(), default))
                for key, default in vars(cls()).items()
            }
        )


def split_text(text: str, settings: ChunkSettings) -> list[str]:
    # Approximate tokens: at most four word characters or one punctuation mark.
    # Offsets retain original whitespace inside chunks, including Cyrillic/CJK.
    spans = list(re.finditer(r"\w{1,4}|[^\w\s]", text, re.UNICODE))
    chunks = []
    for start in range(0, len(spans), settings.size - settings.overlap):
        end = min(start + settings.size, len(spans))
        chunks.append(text[spans[start].start() : spans[end - 1].end()])
        if end == len(spans):
            break
    return chunks


def build_index(
    title: str, text: str, embedder: Embedder, profile: SpaceProfile, settings: ChunkSettings
) -> IndexResult:
    profile.check_embedder(embedder)
    chunks = split_text(text, settings)
    if not chunks:
        raise ValueError("No indexable text")
    vectors = []
    for offset in range(0, len(chunks), settings.batch_size):
        batch = chunks[offset : offset + settings.batch_size]
        result = embedder.documents([f"{title}\n\n{chunk}" for chunk in batch])
        if len(result) != len(batch):
            raise ValueError("Invalid embedding count")
        vectors.extend(result)
    profile.check_embedder(embedder)
    for vector in vectors:
        if (
            len(vector) != profile.dimensions
            or any(type(v) not in (float, int) or not math.isfinite(v) for v in vector)
            or abs(math.hypot(*vector) - 1) > 0.01
        ):
            raise ValueError("Invalid embedding vector")
    return IndexResult(profile, chunks, vectors)


@dataclass(frozen=True)
class ProcessHandler:
    database_url: str
    embedding: EmbeddingSettings
    chunks: ChunkSettings
    embedder_factory: Callable[[EmbeddingSettings], Embedder] = OllamaEmbedder

    enrichment: EnrichmentSettings = field(default_factory=EnrichmentSettings)
    generator_factory: Callable[[LLMSettings], TextGenerator] = OpenAITextGenerator

    def __call__(self, job, context):
        profile = SpaceProfile.from_settings(self.embedding)
        # Autocommit reads end before the HTTP request; no model work in a transaction.
        with psycopg.connect(self.database_url, autocommit=True, row_factory=dict_row) as conn:
            note = conn.execute(
                """SELECT title, extracted_text FROM notes WHERE id=%s
                   AND deleted_at IS NULL AND ingestion_status='ready'""",
                (job.note_id,),
            ).fetchone()
            IndexRepository.check_profile(conn, profile, lock=False)
        if note is None:
            raise ValueError("Note is not ready")
        result = build_index(
            note["title"],
            note["extracted_text"],
            self.embedder_factory(self.embedding),
            profile,
            self.chunks,
        )
        context.publish(result)
        if self.enrichment.enabled:
            llm = LLMSettings.from_env().model_copy(
                update={"max_tokens": self.enrichment.response_tokens}
            )
            context.publish(
                build_enrichment(
                    note["title"],
                    note["extracted_text"],
                    self.generator_factory(llm),
                    llm,
                    self.enrichment,
                )
            )


def publish_process(conn, job, result, *, graph: GraphSettings = GraphSettings()):
    if isinstance(result, IndexResult):
        IndexRepository.publish(conn, job, result, graph=graph)
    elif isinstance(result, EnrichmentResult):
        EnrichmentRepository.publish(conn, job, result)
    else:
        raise TypeError("Unknown process result")


def handlers():
    database_url = os.environ["NOTE_AGENT_DATABASE_URL"]
    return {
        "extract": HandlerBinding(
            ExtractHandler(database_url, Settings.from_env()),
            publish_extract,
            complete_on_publish=True,
        ),
        "process": HandlerBinding(
            ProcessHandler(
                database_url,
                EmbeddingSettings.from_env(),
                ChunkSettings.from_env(),
                enrichment=EnrichmentSettings.from_env(),
            ),
            partial(publish_process, graph=GraphSettings.from_env()),
        ),
        "cleanup": HandlerBinding(
            CleanupHandler(database_url, Settings.from_env().data_dir),
            CleanupRepository.publish,
        ),
    }
