"""Read the original file inventory and clear a deleted note's database content."""

from app.repositories.notes import NotesRepository


class CleanupRepository:
    @staticmethod
    def storage_keys(conn, note_id):
        rows = conn.execute(
            """SELECT a.storage_key FROM attachments a
               JOIN sources s ON s.id = a.source_id
               JOIN notes n ON n.id = s.note_id
               WHERE n.id = %s AND n.deleted_at IS NOT NULL ORDER BY a.storage_key""",
            (note_id,),
        ).fetchall()
        return [row[0] for row in rows]

    @staticmethod
    def publish(conn, job, _payload):
        note_id = job.note_id
        NotesRepository(conn).clear_derived(note_id)
        conn.execute("DELETE FROM collection_notes WHERE note_id = %s", (note_id,))
        conn.execute(
            """DELETE FROM attachments a USING sources s
               WHERE a.source_id = s.id AND s.note_id = %s""",
            (note_id,),
        )
        conn.execute("DELETE FROM sources WHERE note_id = %s AND channel <> 'telegram'", (note_id,))
        # Keep the unique delivery key so a redelivered update cannot resurrect the note.
        conn.execute(
            """UPDATE sources SET type = 'text', original_text = '[deleted]',
                      original_url = NULL, metadata = '{}'
               WHERE note_id = %s AND channel = 'telegram'""",
            (note_id,),
        )
        conn.execute(
            """UPDATE notes SET title = '[deleted]', extracted_text = NULL,
                      title_is_explicit = false, ingestion_status = 'failed',
                      processing_status = 'not_requested', summary = NULL, category = NULL,
                      tags = '[]', entities = '[]', manual_tags = '[]',
                      ingestion_error = NULL, processing_error = NULL,
                      updated_at = clock_timestamp()
               WHERE id = %s""",
            (note_id,),
        )
