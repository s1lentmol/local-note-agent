"""Original materials are immutable during note edits."""


class SourcesRepository:
    def __init__(self, conn):
        self.conn = conn

    def create_text(self, note_id, text):
        self.conn.execute(
            "INSERT INTO sources (note_id, type, original_text) VALUES (%s, 'text', %s)",
            (note_id, text),
        )

    def attachments(self, note_id):
        return self.conn.execute(
            """SELECT a.id, a.original_name, a.mime_type, a.size_bytes,
                      '/api/v1/attachments/' || a.id || '/download' AS download_url
               FROM attachments a JOIN sources s ON s.id = a.source_id
               WHERE s.note_id = %s ORDER BY a.created_at, a.id""",
            (note_id,),
        ).fetchall()
