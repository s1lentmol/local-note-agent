"""Parameterized SQL; callers own transactions and lock notes before jobs."""

from psycopg import Connection


class NotesRepository:
    def __init__(self, conn: Connection):
        self.conn = conn

    def create(self, title, text, explicit):
        return self.conn.execute(
            """INSERT INTO notes (title, extracted_text, title_is_explicit, ingestion_status)
               VALUES (%s, %s, %s, 'ready') RETURNING id""",
            (title, text, explicit),
        ).fetchone()["id"]

    def create_file(self, title, explicit):
        return self.conn.execute(
            """INSERT INTO notes(title, title_is_explicit, ingestion_status)
               VALUES (%s,%s,'queued') RETURNING id""",
            (title, explicit),
        ).fetchone()["id"]

    def lock(self, note_id):
        return self.conn.execute(
            "SELECT * FROM notes WHERE id = %s FOR UPDATE", (note_id,)
        ).fetchone()

    def detail(self, note_id):
        return self.conn.execute(
            """SELECT n.id, n.title, n.extracted_text, n.ingestion_status, n.processing_status,
                      n.summary, n.tags, n.category, n.entities, n.ingestion_error,
                      n.processing_error, n.created_at, n.updated_at,
                      jsonb_build_object('type', s.type, 'original_text', s.original_text,
                        'original_url', s.original_url, 'metadata', s.metadata) AS source
               FROM notes n JOIN sources s ON s.note_id = n.id
               WHERE n.id = %s AND n.deleted_at IS NULL""",
            (note_id,),
        ).fetchone()

    def list(self, query, *, collection_id=None):
        # Only fixed SQL fragments are composed; all values remain parameters.
        clauses = ["n.deleted_at IS NULL"]
        params = []
        for column, value in (
            ("s.type", query.source_type),
            ("n.ingestion_status", query.ingestion_status),
            ("n.processing_status", query.processing_status),
        ):
            if value is not None:
                clauses.append(column + " = %s")
                params.append(value)
        if collection_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM collection_notes cn "
                "WHERE cn.note_id = n.id AND cn.collection_id = %s)"
            )
            params.append(collection_id)
        relation = " FROM notes n JOIN sources s ON s.note_id = n.id WHERE " + " AND ".join(clauses)
        total = self.conn.execute("SELECT count(*) AS total" + relation, params).fetchone()["total"]
        items = self.conn.execute(
            """SELECT n.id, n.title, s.type AS source_type, n.ingestion_status,
                      n.processing_status, n.summary, n.tags, n.created_at, n.updated_at"""
            + relation
            + " ORDER BY n.created_at DESC, n.id DESC LIMIT %s OFFSET %s",
            [*params, query.limit, query.offset],
        ).fetchall()
        return dict(items=items, total=total, limit=query.limit, offset=query.offset)

    def patch(self, note_id, patch):
        self.conn.execute(
            """UPDATE notes SET title = COALESCE(%s, title),
                      title_is_explicit = title_is_explicit OR %s,
                      extracted_text = COALESCE(%s, extracted_text),
                      summary = NULL, category = NULL, tags = '[]', entities = '[]',
                      ingestion_error = NULL, processing_error = NULL,
                      processing_status = 'not_requested', updated_at = clock_timestamp()
               WHERE id = %s""",
            (patch.title, "title" in patch.model_fields_set, patch.extracted_text, note_id),
        )

    def clear_derived(self, note_id):
        for table in ("chunks", "enrichments"):
            self.conn.execute(f"DELETE FROM {table} WHERE note_id = %s", (note_id,))
        self.conn.execute(
            "DELETE FROM note_links WHERE source_note_id = %s OR target_note_id = %s",
            (note_id, note_id),
        )

    def delete(self, note_id):
        self.conn.execute(
            """UPDATE notes SET deleted_at = clock_timestamp(), updated_at = clock_timestamp()
               WHERE id = %s""",
            (note_id,),
        )
