"""Shared process scheduling. Callers lock the note before touching its jobs."""

import argparse
import os

from app.postgres import Database
from app.repositories.notes import NotesRepository


def enqueue_process(conn, note_id) -> bool:
    note = NotesRepository(conn).lock(note_id)
    if note is None or note["deleted_at"] is not None or note["ingestion_status"] != "ready":
        return False
    # Failed notes require explicit retry; old failed jobs after PATCH do not block.
    if note["processing_status"] == "failed":
        return False
    if conn.execute(
        """SELECT 1 FROM jobs WHERE note_id=%s AND kind='process'
           AND status IN ('queued','processing')""",
        (note_id,),
    ).fetchone():
        return False
    conn.execute("INSERT INTO jobs(note_id,kind) VALUES (%s,'process')", (note_id,))
    conn.execute(
        """UPDATE notes SET processing_status='queued', processing_error=NULL,
           updated_at=clock_timestamp() WHERE id=%s""",
        (note_id,),
    )
    return True


def enqueue_ready(database) -> int:
    with database.connection() as conn:
        candidates = conn.execute(
            "SELECT id FROM notes WHERE deleted_at IS NULL AND ingestion_status='ready' ORDER BY id"
        ).fetchall()
    count = 0
    for note in candidates:
        with database.transaction() as conn:
            count += enqueue_process(conn, note["id"])
    return count


def main():
    argparse.ArgumentParser(
        description="Queue process for existing ready notes (no models)"
    ).parse_args()
    with Database(os.environ["NOTE_AGENT_DATABASE_URL"]) as database:
        print(f"Queued: {enqueue_ready(database)}")


if __name__ == "__main__":
    main()
