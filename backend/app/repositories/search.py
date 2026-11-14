"""Exact SQL retrieval for a small corpus; candidate limits count notes, not chunks."""

from app.repositories.index import SpaceProfile


class SearchRepository:
    @staticmethod
    def check_profile(conn, expected, *, lock=False):
        row = conn.execute(
            """SELECT model, revision, dimensions, metric, normalization, query_instruction
               FROM embedding_profiles WHERE id=true"""
            + (" FOR SHARE" if lock else "")
        ).fetchone()
        if row is None or SpaceProfile(**row) != expected:
            raise ValueError("Embedding space profile mismatch")

    @staticmethod
    def retrieve(conn, request, settings, vector=None):
        params = request.model_dump()
        params.update(
            lexical_candidates=settings.lexical_candidates,
            dense_candidates=settings.dense_candidates,
            preview_chars=settings.preview_chars,
            rrf_k=settings.rrf_k,
        )
        # Both channels apply the same filters BEFORE their LIMITs. Inlining lets
        # PostgreSQL use the FTS indexes instead of materializing every full text.
        dense = "SELECT id, 0::double precision AS relevance FROM filtered WHERE false"
        if vector is not None:
            params["vector"] = str(vector)
            dense = """SELECT f.id, -min(c.embedding <=> %(vector)s::vector) AS relevance
                       FROM filtered f JOIN chunks c ON c.note_id=f.id
                       WHERE c.embedding_profile_id=true GROUP BY f.id
                       ORDER BY relevance DESC, f.id LIMIT %(dense_candidates)s"""
        # Materialize TOASTed text before slicing its multibyte preview (PG bug #19406).
        return conn.execute(
            """WITH filtered AS NOT MATERIALIZED (
                SELECT n.id, n.title, n.extracted_text, n.search_ru, n.search_en
                FROM notes n JOIN sources s ON s.note_id=n.id
                WHERE n.deleted_at IS NULL
                  AND (%(source_type)s::text IS NULL OR s.type=%(source_type)s)
                  AND (%(date_from)s::timestamptz IS NULL OR n.created_at >= %(date_from)s)
                  AND (%(date_to)s::timestamptz IS NULL OR n.created_at <= %(date_to)s)
                  AND (%(collection_id)s::uuid IS NULL OR EXISTS (
                      SELECT 1 FROM collection_notes cn
                      WHERE cn.note_id=n.id AND cn.collection_id=%(collection_id)s))
            ), query AS (
                SELECT plainto_tsquery('russian', %(query)s) AS ru,
                       plainto_tsquery('english', %(query)s) AS en
            ), lexical AS (
                SELECT f.id, greatest(ts_rank_cd(search_ru, q.ru),
                                      ts_rank_cd(search_en, q.en)) AS relevance
                FROM filtered f CROSS JOIN query q
                WHERE search_ru @@ q.ru OR search_en @@ q.en
                ORDER BY relevance DESC, f.id LIMIT %(lexical_candidates)s
            ), dense AS ("""
            + dense
            + """), ranks AS (
                SELECT id, 1.0 / (%(rrf_k)s + row_number() OVER (
                    ORDER BY relevance DESC, id)) AS score FROM lexical
                UNION ALL
                SELECT id, 1.0 / (%(rrf_k)s + row_number() OVER (
                    ORDER BY relevance DESC, id)) AS score FROM dense
            ), fused AS (
                SELECT id, sum(score)::double precision AS score FROM ranks GROUP BY id
            )
            SELECT f.id AS note_id, f.title,
                   left(coalesce(f.extracted_text, '') || '', %(preview_chars)s) AS preview, r.score
            FROM fused r JOIN filtered f ON f.id=r.id
            ORDER BY r.score DESC, f.id LIMIT %(limit)s""",
            params,
        ).fetchall()
