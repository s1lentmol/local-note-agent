"""Target HTTP contracts, independent of prototype schemas and future routes."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100000)]


def validate_content(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("Text must contain non-whitespace characters and no NUL")
    return value


OriginalText = Annotated[
    str, StringConstraints(min_length=1, max_length=100000), AfterValidator(validate_content)
]
Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Limit = Annotated[int, Field(ge=1, le=100)]


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def utc_dates(cls, value):
        return value.astimezone(UTC) if isinstance(value, datetime) else value


class SourceType(StrEnum):
    TEXT = "text"
    TEXT_FILE = "text_file"
    URL = "url"
    IMAGE = "image"
    AUDIO = "audio"
    PDF = "pdf"


class IngestionStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class ProcessingStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class JobKind(StrEnum):
    EXTRACT = "extract"
    PROCESS = "process"
    CLEANUP = "cleanup"


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorDetail(DTO):
    code: Nonempty
    message: Nonempty


class ErrorResponse(DTO):
    error: ErrorDetail


class TextNoteCreate(DTO):
    source_type: Literal["text"]
    title: Annotated[Title, AfterValidator(validate_content)] | None = None
    text: OriginalText


class UrlNoteCreate(DTO):
    source_type: Literal["url"]
    title: Title | None = None
    url: HttpUrl


NoteCreate = Annotated[TextNoteCreate | UrlNoteCreate, Field(discriminator="source_type")]


class NotePatch(DTO):
    title: Annotated[Title, AfterValidator(validate_content)] | None = None
    extracted_text: OriginalText | None = None

    @model_validator(mode="after")
    def validate_patch(self):
        if not self.model_fields_set or any(
            getattr(self, field) is None for field in self.model_fields_set
        ):
            raise ValueError("Provide title and/or extracted_text; explicit null is not allowed")
        return self


class RetryRequest(DTO):
    stage: Literal["extract", "process"]


class FileImportFields(DTO):
    """Multipart metadata; the file itself is a transport-level UploadFile."""

    title: Title | None = None
    source_type: Literal["text_file", "audio", "image", "pdf"]


class SourceDetail(DTO):
    type: SourceType
    original_text: str | None = None
    original_url: HttpUrl | None = None
    metadata: dict = Field(default_factory=dict)


class AttachmentDetail(DTO):
    id: UUID
    original_name: str
    mime_type: str
    size_bytes: int = Field(gt=0)
    download_url: Nonempty


class NoteListItem(DTO):
    id: UUID
    title: Title
    source_type: SourceType
    ingestion_status: IngestionStatus
    processing_status: ProcessingStatus
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class NoteDetail(DTO):
    id: UUID
    title: Title
    source: SourceDetail
    extracted_text: str | None = None
    ingestion_status: IngestionStatus
    processing_status: ProcessingStatus
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    entities: list[str] = Field(default_factory=list)
    attachments: list[AttachmentDetail] = Field(default_factory=list)
    ingestion_error: ErrorDetail | None = None
    processing_error: ErrorDetail | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


T = TypeVar("T")


class Page(DTO, Generic[T]):
    items: list[T]
    total: int = Field(ge=0)
    limit: Limit = 20
    offset: int = Field(default=0, ge=0)


class NoteListQuery(DTO):
    limit: Limit = 20
    offset: int = Field(default=0, ge=0)
    source_type: SourceType | None = None
    ingestion_status: IngestionStatus | None = None
    processing_status: ProcessingStatus | None = None


class BatchImportFields(DTO):
    collection_id: UUID | None = None
    collection_title: Title | None = None

    @model_validator(mode="after")
    def exclusive_collection(self):
        if self.collection_id is not None and self.collection_title is not None:
            raise ValueError("collection_id and collection_title are mutually exclusive")
        return self


class ImportAccepted(DTO):
    batch_id: UUID
    collection_id: UUID
    item_ids: list[UUID]


class ImportItem(DTO):
    id: UUID
    original_name: str
    status: Literal["queued", "processing", "done", "failed"]
    note_id: UUID | None = None
    error: ErrorDetail | None = None


class ImportDetail(DTO):
    id: UUID
    collection_id: UUID | None
    status: Literal["queued", "processing", "done", "failed"]
    total: int = Field(gt=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    items: list[ImportItem]

    @model_validator(mode="after")
    def counters(self):
        if self.succeeded + self.failed > self.total:
            raise ValueError("Import counters exceed total")
        return self


class CollectionCreate(DTO):
    title: Title


class CollectionSummary(DTO):
    id: UUID
    title: Title
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CollectionDetail(CollectionSummary):
    notes: Page[NoteListItem]


class CollectionNotesAdd(DTO):
    note_ids: list[UUID] = Field(min_length=1)


class SearchRequest(DTO):
    query: Annotated[Text, AfterValidator(validate_content)]
    source_type: SourceType | None = None
    date_from: AwareDatetime | None = None
    date_to: AwareDatetime | None = None
    collection_id: UUID | None = None
    limit: Limit = 20

    @model_validator(mode="after")
    def date_order(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must precede date_to")
        return self


class SearchItem(DTO):
    note_id: UUID
    title: Title
    preview: str
    score: float = Field(allow_inf_nan=False)


class SearchResponse(DTO):
    items: list[SearchItem]


class ChatRequest(DTO):
    message: Text
    conversation_id: UUID | None = None
    collection_id: UUID | None = None


class Operation(DTO):
    type: Literal[
        "save_note",
        "update_note",
        "create_collection",
        "add_to_collection",
        "remove_from_collection",
        "delete_collection",
    ]
    id: UUID
    note_id: UUID | None = None


class ChatResponse(DTO):
    conversation_id: UUID
    text: str
    operations: list[Operation] = Field(default_factory=list)


class GraphQuery(DTO):
    note_id: UUID | None = None
    collection_id: UUID | None = None
    limit: Limit = 20


class GraphNode(DTO):
    id: UUID
    title: Title


class GraphEdge(DTO):
    source: UUID
    target: UUID
    score: float = Field(ge=-1, le=1, allow_inf_nan=False)


class GraphResponse(DTO):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class NoteDocument(DTO):
    note_id: UUID
    title: Title
    text: Text


class InternalChunk(DTO):
    text: Nonempty
    order: int = Field(ge=0)
    embedding: list[Annotated[float, Field(allow_inf_nan=False)]] = Field(
        min_length=1024, max_length=1024
    )


class Enrichment(DTO):
    summary: Nonempty
    tags: list[str]
    category: str | None
    entities: list[str]
