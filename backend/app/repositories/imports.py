"""Batch and item persistence. Extraction progress is read from T11 state."""

from psycopg.types.json import Jsonb


class ImportsRepository:
    def __init__(self, conn):
        self.conn = conn

    def lock_delivery(self, scope, external_id):
        # Only Telegram batches with the same identity need to serialize creation.
        self.conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"telegram-import:{scope}:{external_id}",),
        )

    def telegram_batch(self, scope, external_id):
        return self.conn.execute(
            """SELECT id, collection_id, total FROM import_batches
               WHERE channel='telegram' AND delivery_scope=%s AND external_id=%s""",
            (scope, external_id),
        ).fetchone()

    def deliveries(self, batch_id):
        return {
            row["delivery_id"]
            for row in self.conn.execute(
                "SELECT delivery_id FROM import_items WHERE batch_id=%s", (batch_id,)
            )
        }

    def create(self, collection_id, total, scope, external_id):
        return self.conn.execute(
            """INSERT INTO import_batches (collection_id, total, channel, delivery_scope, external_id)
               VALUES (%s,%s,%s,%s,%s) RETURNING id""",
            (collection_id, total, "web" if scope is None else "telegram", scope, external_id),
        ).fetchone()["id"]

    def extend(self, batch_id, count):
        self.conn.execute(
            """UPDATE import_batches SET total=total+%s, updated_at=clock_timestamp()
               WHERE id=%s""",
            (count, batch_id),
        )

    def create_item(self, batch_id, position, name, delivery_id, note_id, error):
        return self.conn.execute(
            """INSERT INTO import_items
                   (batch_id, position, original_name, delivery_id, note_id, status, error)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (
                batch_id,
                position,
                name,
                delivery_id,
                note_id,
                "failed" if error else "queued",
                Jsonb(error) if error else None,
            ),
        ).fetchone()["id"]

    def get(self, batch_id):
        return self.conn.execute(
            "SELECT id, collection_id, total FROM import_batches WHERE id=%s", (batch_id,)
        ).fetchone()

    def items(self, batch_id):
        return self.conn.execute(
            """SELECT i.id, i.original_name, i.status, i.error, i.note_id,
                      n.deleted_at, n.ingestion_status, n.ingestion_error,
                      extract.status AS extract_status, extract.error AS extract_error
               FROM import_items i
               LEFT JOIN notes n ON n.id=i.note_id
               LEFT JOIN LATERAL (
                   SELECT status, error FROM jobs
                   WHERE note_id=i.note_id AND kind='extract' AND n.deleted_at IS NOT NULL
                   ORDER BY created_at DESC, id DESC LIMIT 1
               ) extract ON true
               WHERE i.batch_id=%s ORDER BY i.position""",
            (batch_id,),
        ).fetchall()
