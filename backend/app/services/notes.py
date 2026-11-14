"""Shared CRUD application layer. No HTTP, extraction or model dependencies."""

from uuid import UUID

from psycopg import Connection

from app.dto import NoteDetail, NoteListItem, NoteListQuery, NotePatch, Page, TextNoteCreate
from app.jobs_repository import JobsRepository
from app.postgres import Database
from app.repositories.notes import NotesRepository
from app.repositories.sources import DuplicateDelivery, SourcesRepository, TelegramDelivery
from app.services.processing import enqueue_process


class NoteError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class NotesService:
    def __init__(self, database: Database, *, processing_enabled: bool = False):
        self.database = database
        self.processing_enabled = processing_enabled

    def _detail(self, conn: Connection, note_id: UUID) -> NoteDetail:
        row = NotesRepository(conn).detail(note_id)
        if row is None:
            raise NoteError("note_not_found", "Заметка не найдена.")
        row["attachments"] = SourcesRepository(conn).attachments(note_id)
        return NoteDetail.model_validate(row)

    def create(
        self, request: TextNoteCreate, *, delivery: TelegramDelivery | None = None
    ) -> NoteDetail:
        request = TextNoteCreate.model_validate(request)
        title = request.title or request.text.strip().splitlines()[0].strip()[:200]
        with self.database.transaction() as conn:
            sources = SourcesRepository(conn)
            try:
                # Savepoint removes the losing note as well as any derived writes.
                # PostgreSQL's unique constraint waits for concurrent deliveries.
                with conn.transaction():
                    note_id = NotesRepository(conn).create(
                        title, request.text, request.title is not None
                    )
                    if delivery is None:
                        sources.create_text(note_id, request.text)
                    else:
                        sources.create_telegram_text(note_id, request.text, delivery)
                    if self.processing_enabled:
                        enqueue_process(conn, note_id)
            except DuplicateDelivery:
                note_id = sources.telegram_note_id(delivery)
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
            repo.patch(note_id, patch)
            JobsRepository.cancel_active(conn, note_id)
            repo.clear_derived(note_id)
            if self.processing_enabled:
                enqueue_process(conn, note_id)
            return self._detail(conn, note_id)

    def delete(self, note_id: UUID) -> None:
        with self.database.transaction() as conn:
            repo = NotesRepository(conn)
            note = repo.lock(note_id)
            if note is None:
                raise NoteError("note_not_found", "Заметка не найдена.")
            jobs = JobsRepository(self.database)
            if note["deleted_at"] is None:
                repo.delete(note_id)
                jobs.cancel_active(conn, note_id)
                jobs.enqueue_cleanup(conn, note_id)
            else:
                jobs.retry_latest(conn, note_id, "cleanup")

    def retry(self, note_id: UUID, stage: str) -> NoteDetail:
        with self.database.transaction() as conn:
            note = NotesRepository(conn).lock(note_id)
            if note is None or note["deleted_at"] is not None:
                raise NoteError("note_not_found", "Заметка не найдена.")
            available = (stage == "extract" and note["ingestion_status"] == "failed") or (
                stage == "process"
                and note["ingestion_status"] == "ready"
                and note["processing_status"] == "failed"
            )
            if not available:
                raise NoteError("retry_not_available", "Повтор обработки сейчас недоступен.")
            if not JobsRepository(self.database).retry_latest(conn, note_id, stage):
                raise NoteError("retry_not_available", "Повтор обработки сейчас недоступен.")
            return self._detail(conn, note_id)
