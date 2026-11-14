"""SQL index publication, invoked only inside JobsRepository.publish."""

from dataclasses import dataclass

from app.graph_settings import GraphSettings
from app.repositories.graph import GraphRepository


@dataclass(frozen=True)
class SpaceProfile:
    model: str
    revision: str
    dimensions: int
    metric: str
    normalization: str
    query_instruction: str

    @classmethod
    def from_settings(cls, settings):
        return cls(
            settings.model,
            settings.digest,
            settings.dimensions,
            "cosine",
            "l2",
            f"Instruct: {settings.query_instruction}\nQuery: ",
        )

    def check_embedder(self, embedder):
        if (
            embedder.identity != f"{self.model}@sha256:{self.revision}"
            or embedder.dimensions != self.dimensions
        ):
            raise ValueError("Embedding identity or dimensions mismatch")


@dataclass(frozen=True)
class IndexResult:
    profile: SpaceProfile
    chunks: list[str]
    vectors: list[list[float]]


class IndexRepository:
    @staticmethod
    def check_profile(conn, expected, *, lock):
        row = conn.execute(
            """SELECT model, revision, dimensions, metric, normalization, query_instruction
               FROM embedding_profiles WHERE id=true"""
            + (" FOR UPDATE" if lock else "")
        ).fetchone()
        if row is None or SpaceProfile(**row) != expected:
            raise ValueError("Embedding space profile mismatch")

    @staticmethod
    def publish(conn, job, result: IndexResult, *, graph: GraphSettings = GraphSettings()):
        # T06 already holds note then job locks. Serialize against profile changes
        # BEFORE deleting old chunks, and retain the lock until the transaction ends.
        IndexRepository.check_profile(conn, result.profile, lock=True)
        if not result.chunks or len(result.chunks) != len(result.vectors):
            raise ValueError("Invalid index result")
        conn.execute("DELETE FROM chunks WHERE note_id=%s", (job.note_id,))
        with conn.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO chunks(note_id,job_id,position,text,embedding)
                   VALUES (%s,%s,%s,%s,%s::vector)""",
                [
                    (job.note_id, job.id, pos, text, str(vector))
                    for pos, (text, vector) in enumerate(zip(result.chunks, result.vectors))
                ],
            )
        GraphRepository.replace_links(conn, job.note_id, graph)
