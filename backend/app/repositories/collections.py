"""Collections and membership queries; callers own transactions."""


class CollectionsRepository:
    def __init__(self, conn):
        self.conn = conn

    def create(self, title):
        return self.conn.execute(
            "INSERT INTO collections(title) VALUES (%s) RETURNING *", (title,)
        ).fetchone()

    def get(self, collection_id):
        return self.conn.execute(
            "SELECT * FROM collections WHERE id = %s", (collection_id,)
        ).fetchone()

    def lock(self, collection_id):
        return self.conn.execute(
            "SELECT id FROM collections WHERE id = %s FOR KEY SHARE", (collection_id,)
        ).fetchone()

    def list(self):
        return self.conn.execute(
            "SELECT * FROM collections ORDER BY created_at DESC, id DESC"
        ).fetchall()

    def add(self, collection_id, note_ids):
        self.conn.execute(
            """INSERT INTO collection_notes (collection_id, note_id)
               SELECT %s, id FROM unnest(%s::uuid[]) AS ids(id) ORDER BY id
               ON CONFLICT (collection_id, note_id) DO NOTHING""",
            (collection_id, note_ids),
        )

    def lock_live_notes(self, note_ids):
        return self.conn.execute(
            """SELECT id FROM notes WHERE id = ANY(%s::uuid[]) AND deleted_at IS NULL
               ORDER BY id FOR SHARE""",
            (note_ids,),
        ).fetchall()

    def remove(self, collection_id, note_id):
        self.conn.execute(
            "DELETE FROM collection_notes WHERE collection_id = %s AND note_id = %s",
            (collection_id, note_id),
        )

    def delete(self, collection_id):
        return self.conn.execute("DELETE FROM collections WHERE id = %s", (collection_id,)).rowcount
