"""Extract in the spawned worker; publish only through the shared lease guard."""

from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row

from app.config import Settings
from app.extractors.audio import transcribe_audio, validate_audio
from app.extractors.text import extract_text, text_error
from app.model_clients import ASRSettings, HTTPTranscriber, Transcriber
from app.repositories.attachments import AttachmentsRepository
from app.repositories.sources import SourcesRepository
from app.services.processing import enqueue_process
from app.services.storage import Storage


@dataclass(frozen=True)
class ExtractResult:
    text: str
    processing_enabled: bool
    language: str | None = None


@dataclass(frozen=True)
class ExtractHandler:
    database_url: str
    settings: Settings
    transcriber: Transcriber | None = None

    def __call__(self, job, context):
        with psycopg.connect(self.database_url, autocommit=True, row_factory=dict_row) as conn:
            original = AttachmentsRepository(conn).extract_original(job.note_id)
        if original is None:
            raise text_error("original_missing")
        # The read connection is closed before opening/decoding the original.
        path = Storage(self.settings.data_dir).path(original["storage_key"])
        try:
            result = self.extract(path, original["type"])
        except FileNotFoundError:
            raise text_error("original_missing") from None
        context.publish(result)

    def extract(self, path, source_type):
        if source_type == "audio":
            validate_audio(path, self.settings.audio_max_bytes, self.settings.audio_max_seconds)
            client = self.transcriber
            if client is None:
                client = HTTPTranscriber(ASRSettings.from_env())
            transcript = transcribe_audio(path, client, self.settings.text_max_chars)
            return ExtractResult(
                transcript.text, self.settings.processing_enabled, transcript.language
            )
        with path.open("rb") as stream:
            data = stream.read(self.settings.text_file_max_bytes + 1)
        if len(data) > self.settings.text_file_max_bytes:
            raise text_error("file_too_large")
        text = extract_text(data, self.settings.text_max_chars)
        return ExtractResult(text, self.settings.processing_enabled)


def publish_extract(conn, job, result):
    if job.kind != "extract" or not isinstance(result, ExtractResult):
        raise TypeError("Invalid extract publication")
    conn.execute(
        """UPDATE notes SET extracted_text=%s, ingestion_status='ready', ingestion_error=NULL,
           updated_at=clock_timestamp() WHERE id=%s""",
        (result.text, job.note_id),
    )
    if result.language is not None:
        SourcesRepository(conn).set_language(job.note_id, result.language)
    if result.processing_enabled:
        enqueue_process(conn, job.note_id)
