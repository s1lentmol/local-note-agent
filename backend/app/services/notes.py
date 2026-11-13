"""Shared CRUD application layer. No HTTP, extraction or model dependencies."""

from uuid import UUID

from psycopg import Connection

from app.dto import NoteDetail, NoteListItem, NoteListQuery, NotePatch, Page, TextNoteCreate
from app.postgres import Database
from app.repositories.notes import NotesRepository
from app.repositories.sources import SourcesRepository


class NoteError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class NotesService:
    def __init__(self, database: Database):
        self.database = database

    def _detail(self, conn: Connection, note_id: UUID) -> NoteDetail:
        row = NotesRepository(conn).detail(note_id)
        if row is None:
            raise NoteError("note_not_found", "Заметка не найдена.")
        row["attachments"] = SourcesRepository(conn).attachments(note_id)
        return NoteDetail.model_validate(row)

    def create(self, request: TextNoteCreate) -> NoteDetail:
        request = TextNoteCreate.model_validate(request)
        title = request.title or request.text.strip().splitlines()[0].strip()[:200]
        with self.database.transaction() as conn:
            note_id = NotesRepository(conn).create(title, request.text, request.title is not None)
            SourcesRepository(conn).create_text(note_id, request.text)
            return self._detail(conn, note_id)

    def get(self, note_id: UUID) -> NoteDetail:
        with self.database.transaction() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            return self._detail(conn, note_id)

    def list(self, query: NoteListQuery) -> Page[NoteListItem]:
        query = NoteListQuery.model_validate(query)
        with self.database.transaction() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            return Page[NoteListItem].model_validate(NotesRepository(conn).list(query))

    def patch(self, note_id: UUID, patch: NotePatch) -> NoteDetail:
        patch = NotePatch.model_validate(patch)
        with self.database.transaction() as conn:
            repo = NotesRepository(conn)
            note = repo.lock(note_id)
            if note is None or note["deleted_at"] is not None:
                raise NoteError("note_not_found", "Заметка не найдена.")
            if note["ingestion_status"] != "ready":
                raise NoteError("note_not_ready", "Извлечение текста ещё не завершено.")
            repo.cancel_jobs(note_id)
            repo.patch(note_id, patch)
            return self._detail(conn, note_id)

    def delete(self, note_id: UUID) -> None:
        with self.database.transaction() as conn:
            repo = NotesRepository(conn)
            note = repo.lock(note_id)
            if note is None:
                raise NoteError("note_not_found", "Заметка не найдена.")
            if note["deleted_at"] is not None:
                # A repeated DELETE leaves an active cleanup alone; a failed cleanup can retry.
                active = repo.active_cleanup(note_id)
                if active is None:
                    repo.queue_cleanup(note_id)
                return
            repo.cancel_jobs(note_id)
            repo.delete(note_id)
            repo.queue_cleanup(note_id)
