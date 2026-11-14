"""Immutable file records. Transactions belong to the application service."""


class AttachmentsRepository:
    def __init__(self, conn):
        self.conn = conn

    def create(self, attachment_id, source_id, name, key, mime, size):
        self.conn.execute(
            """INSERT INTO attachments
               (id, source_id, original_name, storage_key, mime_type, size_bytes)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (attachment_id, source_id, name, key, mime, size),
        )

    def download(self, attachment_id):
        return self.conn.execute(
            """SELECT a.* FROM attachments a JOIN sources s ON s.id=a.source_id
               JOIN notes n ON n.id=s.note_id WHERE a.id=%s AND n.deleted_at IS NULL""",
            (attachment_id,),
        ).fetchone()

    def extract_original(self, note_id):
        return self.conn.execute(
            """SELECT a.storage_key, s.type FROM attachments a JOIN sources s ON s.id=a.source_id
               JOIN notes n ON n.id=s.note_id
               WHERE s.note_id=%s AND s.type IN ('text_file', 'audio') AND n.deleted_at IS NULL""",
            (note_id,),
        ).fetchone()
