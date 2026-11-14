"""Semantic link publication and bounded graph reads using PostgreSQL/pgvector."""

from app.dto import GraphQuery, GraphResponse
from app.graph_settings import GraphSettings

CALCULATION_SOURCE = "embedding-centroid-cosine-v1"


class GraphRepository:
    @staticmethod
    def replace_links(conn, note_id, settings: GraphSettings):
        candidates = conn.execute(
            """
            WITH centroids AS (
                SELECT c.note_id, avg(c.embedding ORDER BY c.position) AS embedding
                FROM chunks c
                JOIN notes n ON n.id = c.note_id
                WHERE c.embedding_profile_id = true
                  AND n.deleted_at IS NULL AND n.ingestion_status = 'ready'
                GROUP BY c.note_id
            ), scores AS (
                SELECT other.note_id,
                       CASE WHEN vector_norm(origin.embedding) > 0
                             AND vector_norm(other.embedding) > 0
                            THEN 1 - (origin.embedding <=> other.embedding)
                       END AS score
                FROM centroids origin
                CROSS JOIN centroids other
                WHERE origin.note_id = %(note)s AND other.note_id <> %(note)s
            )
            SELECT n.id, s.score
            FROM scores s JOIN notes n ON n.id = s.note_id
            WHERE s.score BETWEEN %(min_score)s AND 1
              AND n.deleted_at IS NULL AND n.ingestion_status = 'ready'
            ORDER BY s.score DESC, n.id
            LIMIT %(top_k)s
            FOR SHARE OF n SKIP LOCKED
            """,
            {"note": note_id, "min_score": settings.min_score, "top_k": settings.top_k},
        ).fetchall()

        # T06 holds the publishing note and job, and the index holds the singleton
        # profile. Never wait for another note lock here. A PATCH may also commit
        # between scoring and taking the share lock, so check its chunks again
        # with a fresh snapshot. Other index writers serialize on the profile.
        conn.execute(
            "DELETE FROM note_links WHERE source_note_id = %s OR target_note_id = %s",
            (note_id, note_id),
        )
        if candidates:
            conn.execute(
                """
                INSERT INTO note_links (source_note_id, target_note_id, score, calculation_source)
                SELECT least(%(note)s, candidate.id), greatest(%(note)s, candidate.id),
                       candidate.score, %(source)s
                FROM unnest(%(ids)s::uuid[], %(scores)s::float8[]) AS candidate(id, score)
                WHERE EXISTS (
                    SELECT 1 FROM chunks c
                    WHERE c.note_id = candidate.id AND c.embedding_profile_id = true
                )
                """,
                {
                    "note": note_id,
                    "source": CALCULATION_SOURCE,
                    "ids": [row["id"] for row in candidates],
                    "scores": [row["score"] for row in candidates],
                },
            )

    @staticmethod
    def read(conn, query: GraphQuery) -> GraphResponse:
        # A single statement keeps vertex selection and edge visibility on one snapshot.
        row = conn.execute(
            """
            WITH eligible AS (
                SELECT n.id, n.title FROM notes n
                WHERE n.deleted_at IS NULL AND n.ingestion_status = 'ready'
                  AND (%(collection)s::uuid IS NULL OR EXISTS (
                    SELECT 1 FROM collection_notes c
                    WHERE c.collection_id = %(collection)s AND c.note_id = n.id
                  ))
            ), visible_links AS (
                SELECT l.source_note_id AS source, l.target_note_id AS target, l.score
                FROM note_links l
                JOIN eligible s ON s.id = l.source_note_id
                JOIN eligible t ON t.id = l.target_note_id
            ), ranked_links AS (
                SELECT source, target, row_number() OVER (ORDER BY score DESC, source, target) AS rank
                FROM visible_links
                WHERE %(note)s::uuid IS NULL OR %(note)s IN (source, target)
            ), ranked_nodes AS (
                SELECT source AS id, rank FROM ranked_links
                UNION ALL
                SELECT target AS id, rank FROM ranked_links
            ), chosen AS (
                SELECT e.id, e.title, e.id IS NOT DISTINCT FROM %(note)s::uuid AS anchor,
                       min(r.rank) AS rank
                FROM eligible e LEFT JOIN ranked_nodes r ON r.id = e.id
                WHERE e.id = %(note)s OR r.id IS NOT NULL
                GROUP BY e.id, e.title
                ORDER BY anchor DESC, rank NULLS LAST, e.id
                LIMIT %(limit)s
            )
            SELECT
                COALESCE((
                    SELECT jsonb_agg(jsonb_build_object('id', id, 'title', title)
                                     ORDER BY anchor DESC, rank NULLS LAST, id)
                    FROM chosen
                ), '[]') AS nodes,
                COALESCE((
                    SELECT jsonb_agg(to_jsonb(l) ORDER BY l.score DESC, l.source, l.target)
                    FROM visible_links l
                    JOIN chosen s ON s.id = l.source
                    JOIN chosen t ON t.id = l.target
                ), '[]') AS edges
            """,
            {"note": query.note_id, "collection": query.collection_id, "limit": query.limit},
        ).fetchone()
        return GraphResponse.model_validate(row)
