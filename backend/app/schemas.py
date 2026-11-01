from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from app.integrations.knowledge import Entity

SourceType = Literal["text", "url", "image", "audio"]
IngestionStatus = Literal["queued", "processing", "ready", "failed"]
EnrichmentStatus = Literal["not_requested", "queued", "processing", "ready", "failed"]


def reject_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("Text must not be blank")
    return value


Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
NoteText = Annotated[
    str, StringConstraints(min_length=1, max_length=100_000), AfterValidator(reject_blank)
]


class TextNoteCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "source_type": "text",
                    "title": "Конспект",
                    "text": "Текст заметки",
                }
            ]
        },
    )
    source_type: Literal["text"]
    title: Title
    text: NoteText = Field(description="Исходный текст. Сохраняется без изменения.")


class UrlNoteCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "source_type": "url",
                    "title": "Статья",
                    "url": "https://example.com/",
                }
            ]
        },
    )
    source_type: Literal["url"]
    title: Title
    url: AnyHttpUrl = Field(
        max_length=2048, description="Публичная HTML-страница без авторизации. Порты 80 и 443."
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        if value.username is not None or value.password is not None or value.port not in (80, 443):
            raise ValueError("Credentials and nonstandard ports are not supported")
        return value


NoteCreate = Annotated[TextNoteCreate | UrlNoteCreate, Field(discriminator="source_type")]


class NoteUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "expected_version": 1,
                    "title": "Обновлённый конспект",
                    "extracted_text": "Исправленный текст",
                }
            ]
        },
    )
    expected_version: int = Field(
        ge=1, strict=True, description="Актуальная content_version из GET заметки."
    )
    title: Title | SkipJsonSchema[None] = Field(
        default=None, description="Новый заголовок. Пропустить можно, передавать null нельзя."
    )
    extracted_text: NoteText | SkipJsonSchema[None] = Field(
        default=None, description="Новый текст. Пропустить можно, передавать null нельзя."
    )

    @model_validator(mode="after")
    def require_changes(self) -> Self:
        fields = self.model_fields_set - {"expected_version"}
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError("Provide a non-null title or extracted_text")
        return self


class ErrorDetail(BaseModel):
    code: str = Field(description="Машинный код ошибки.")
    message: str = Field(description="Описание ошибки.")
    fields: list[list[str | int]] | None = Field(
        default=None, description="Пути к некорректным полям запроса."
    )


class ErrorResponse(BaseModel):
    error: ErrorDetail


class Timestamped(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    created_at: datetime = Field(description="Время создания в UTC.")

    @field_validator("created_at", "updated_at", "started_at", "finished_at", check_fields=False)
    @classmethod
    def restore_utc(cls, value):
        return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


class SourceDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    type: SourceType
    original_text: str | None = Field(
        description="Оригинал введённого текста; null для ссылки и файла."
    )
    original_url: str | None
    metadata: dict = Field(
        validation_alias="metadata_json",
        description="Метаданные источника: URL, язык, размеры или длительность.",
    )


class AttachmentDetail(Timestamped):
    id: UUID
    original_name: str
    mime_type: str
    size_bytes: int
    download_url: str = Field(description="Относительный адрес скачивания оригинала.")


class NoteSummary(Timestamped):
    id: UUID
    title: str
    source_type: SourceType
    ingestion_status: IngestionStatus = Field(description="Состояние извлечения текста.")
    enrichment_status: EnrichmentStatus = Field(
        description="Состояние обработки модулем знаний; not_requested, если адаптер выключен."
    )
    summary: str | None
    tags: list[str]
    category: str | None
    updated_at: datetime = Field(description="Время последнего изменения в UTC.")


class NoteDetail(NoteSummary):
    source: SourceDetail
    attachments: list[AttachmentDetail]
    extracted_text: str | None = Field(
        description="Текущий текст заметки; null до успешного импорта."
    )
    content_version: int = Field(
        description="Версия заголовка и текста. У импортируемой заметки до извлечения — 0."
    )
    enrichment_version: int | None
    entities: list[Entity]
    ingestion_error: ErrorDetail | None
    enrichment_error: ErrorDetail | None


class NoteList(BaseModel):
    items: list[NoteSummary]
    total: int = Field(description="Общее число заметок с учётом фильтров.")
    limit: int
    offset: int


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Literal["extract", "enrich"] = Field(
        description="Стадия в состоянии failed, которую нужно повторить."
    )


class JobDetail(Timestamped):
    id: UUID
    note_id: UUID
    kind: Literal["extract", "enrich", "cleanup"]
    input_version: int
    status: Literal["queued", "processing", "done", "failed", "cancelled"]
    attempts: int
    error: ErrorDetail | None
    started_at: datetime | None
    finished_at: datetime | None


class ReindexResponse(BaseModel):
    queued: int = Field(description="Количество новых заданий обработки знаний.")


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    database: bool
    worker: bool = Field(description="Worker отправлял heartbeat в последние 15 секунд.")
    capabilities: dict[str, bool] = Field(
        description="Доступность OCR, модели аудио и адаптера в окружении worker."
    )


class CapabilitiesResponse(BaseModel):
    source_types: list[str]
    max_image_bytes: int
    max_audio_bytes: int
    max_audio_seconds: int
    max_image_pixels: int
    max_text_chars: int
    max_html_bytes: int
    knowledge_enabled: bool
    polling_interval_seconds: int
