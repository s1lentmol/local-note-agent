"""Temporary voice input: only a committed save adopts the original."""

from contextlib import contextmanager
from dataclasses import dataclass

from app.dto import ChatRequest, FileImportFields
from app.extractors.audio import transcribe_audio, validate_audio
from app.model_clients.settings import ASRSettings
from app.model_clients.transcription import HTTPTranscriber
from app.repositories.attachments import AttachmentsRepository
from app.repositories.sources import SourcesRepository, TelegramDelivery
from app.services.file_import import StoredFile
from app.services.storage import Storage


@dataclass
class VoiceInput:
    original: StoredFile
    language: str
    delivery: TelegramDelivery

    def attach(self, conn, note_id):
        # NotesService owns creation/lifecycle; convert its source to audio and
        # attach the already validated original in the same action transaction.
        source_id = SourcesRepository(conn).adopt_voice(note_id, self.delivery, self.language)
        o = self.original
        AttachmentsRepository(conn).create(o.id, source_id, o.name, o.key, o.mime, o.size)


class VoiceChatService:
    def __init__(self, chat, files, transcriber=None):
        self.chat, self.files, self.transcriber = chat, files, transcriber

    @contextmanager
    def prepare(self, stream, filename, delivery):
        with self.files.stage(stream, filename, FileImportFields(source_type="audio")) as original:
            settings = self.files.settings
            path = Storage(settings.data_dir).path(original.key)
            validate_audio(path, settings.audio_max_bytes, settings.audio_max_seconds)
            transcript = transcribe_audio(
                path,
                self.transcriber or HTTPTranscriber(ASRSettings.from_env()),
                settings.text_max_chars,
            )
            yield transcript.text, VoiceInput(original, transcript.language, delivery)

    def reply(self, stream, filename, delivery, chat_delivery):
        cached = self.chat.delivered(chat_delivery)
        if cached is not None:
            return cached
        with self.prepare(stream, filename, delivery) as (text, voice):
            return self.chat.reply(ChatRequest(message=text), delivery=chat_delivery, voice=voice)
