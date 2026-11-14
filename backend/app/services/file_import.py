"""Single-file import; Storage IO is outside the atomic SQL creation."""

from pathlib import Path
from uuid import uuid4

from app.dto import FileImportFields
from app.errors import ProcessingError
from app.repositories.attachments import AttachmentsRepository
from app.repositories.notes import NotesRepository
from app.repositories.sources import SourcesRepository
from app.services.notes import NoteError, NotesService
from app.services.storage import Storage


class FileImportService:
    def __init__(self, database, settings):
        self.database = database
        self.settings = settings

    def import_file(self, stream, filename, fields: FileImportFields):
        fields = FileImportFields.model_validate(fields)
        if fields.source_type != "text_file":
            raise ProcessingError("unsupported_format", "Поддерживаются только TXT и Markdown.")
        name = filename or ""
        if (
            not name
            or "\x00" in name
            or Path(name).suffix.lower() not in {".txt", ".md", ".markdown"}
        ):
            raise ProcessingError("unsupported_format", "Поддерживаются только TXT и Markdown.")
        title = fields.title or name.replace("\\", "/").rsplit("/", 1)[-1].strip()[:200]
        if not title or "\x00" in title:
            raise ProcessingError("invalid_title", "Некорректный заголовок.")
        attachment_id = uuid4()
        storage = Storage(self.settings.data_dir)
        key = None
        try:
            key, size = storage.save(stream, str(attachment_id), self.settings.text_file_max_bytes)
            with self.database.transaction() as conn:
                note_id = NotesRepository(conn).create_file(title, fields.title is not None)
                source_id = SourcesRepository(conn).create_file(note_id)
                mime = "text/plain" if Path(name).suffix.lower() == ".txt" else "text/markdown"
                AttachmentsRepository(conn).create(attachment_id, source_id, name, key, mime, size)
                conn.execute("INSERT INTO jobs(note_id,kind) VALUES (%s,'extract')", (note_id,))
                result = NotesService(self.database)._detail(conn, note_id)
            return result
        except BaseException:
            if key is not None:
                storage.remove(key)
            raise

    def download(self, attachment_id):
        with self.database.transaction() as conn:
            record = AttachmentsRepository(conn).download(attachment_id)
        if record is None:
            raise NoteError("attachment_not_found", "Вложение не найдено.")
        path = Storage(self.settings.data_dir).path(record["storage_key"])
        try:
            stream = path.open("rb")
        except (FileNotFoundError, IsADirectoryError):
            raise NoteError("attachment_not_found", "Вложение не найдено.") from None
        return stream, record
