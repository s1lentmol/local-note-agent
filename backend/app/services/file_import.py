"""Single-file import; Storage IO is outside the atomic SQL creation."""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from app.dto import FileImportFields
from app.errors import ProcessingError
from app.repositories.attachments import AttachmentsRepository
from app.repositories.notes import NotesRepository
from app.repositories.sources import SourcesRepository
from app.services.notes import NoteError, NotesService
from app.services.storage import Storage

TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
AUDIO_SUFFIXES = {".ogg", ".oga", ".opus", ".wav", ".mp3"}
AUDIO_MIME_TYPES = {
    "audio/ogg",
    "application/ogg",
    "audio/opus",
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/vnd.wave",
    "audio/mpeg",
    "audio/mp3",
}


def infer_source_type(filename, mime=None):
    # Names and MIME route the upload; the extract worker validates the bytes.
    suffix = Path(filename or "").suffix.lower()
    if suffix not in TEXT_SUFFIXES and (
        suffix in AUDIO_SUFFIXES
        or (mime or "").partition(";")[0].strip().lower() in AUDIO_MIME_TYPES
    ):
        return "audio"
    return "text_file"


def text_filename(filename):
    name = filename or ""
    if not name or "\x00" in name or Path(name).suffix.lower() not in TEXT_SUFFIXES:
        raise ProcessingError(
            "unsupported_format", "Поддерживаются TXT, Markdown, OGG/Opus, WAV и MP3."
        )
    return name


@dataclass
class StoredFile:
    id: UUID
    name: str
    title: str
    explicit: bool
    key: str
    mime: str
    size: int
    source_type: str
    committed: bool = False


class FileImportService:
    def __init__(self, database, settings):
        self.database = database
        self.settings = settings

    def import_file(self, stream, filename, fields: FileImportFields):
        with self.stage(stream, filename, fields) as original:
            with self.database.transaction() as conn:
                note_id = self.create_note(conn, original)
                result = NotesService(self.database)._detail(conn, note_id)
            original.committed = True
            return result

    def max_bytes(self, source_type):
        if source_type == "audio":
            return self.settings.audio_max_bytes
        return self.settings.text_file_max_bytes

    @contextmanager
    def stage(self, stream, filename, fields: FileImportFields):
        fields = FileImportFields.model_validate(fields)
        source_type = fields.source_type
        if source_type == "text_file":
            name = text_filename(filename)
            mime = "text/plain" if Path(name).suffix.lower() == ".txt" else "text/markdown"
        elif source_type == "audio":
            name = filename or "Аудиозапись"
            mime = "application/octet-stream"
        else:
            raise ProcessingError(
                "unsupported_format", "Поддерживаются TXT, Markdown, OGG/Opus, WAV и MP3."
            )
        if "\x00" in name:
            raise ProcessingError("invalid_filename", "Некорректное имя файла.")
        title = fields.title or name.replace("\\", "/").rsplit("/", 1)[-1].strip()[:200]
        if not title or "\x00" in title:
            raise ProcessingError("invalid_title", "Некорректный заголовок.")
        attachment_id = uuid4()
        storage = Storage(self.settings.data_dir)
        key, size = storage.save(stream, str(attachment_id), self.max_bytes(source_type))
        original = StoredFile(
            attachment_id, name, title, fields.title is not None, key, mime, size, source_type
        )
        try:
            yield original
        finally:
            if not original.committed:
                storage.remove(key)

    @staticmethod
    def create_note(conn, original: StoredFile, *, delivery=None):
        note_id = NotesRepository(conn).create_file(original.title, original.explicit)
        sources = SourcesRepository(conn)
        source_id = (
            sources.create_file(note_id, original.source_type)
            if delivery is None
            else sources.create_telegram_file(note_id, delivery, original.source_type)
        )
        AttachmentsRepository(conn).create(
            original.id, source_id, original.name, original.key, original.mime, original.size
        )
        conn.execute("INSERT INTO jobs(note_id,kind) VALUES (%s,'extract')", (note_id,))
        return note_id

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
