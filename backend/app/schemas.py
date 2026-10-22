from datetime import datetime, timezone
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, StringConstraints,
    field_validator, model_validator,
)


def reject_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("Text must not be blank")
    return value


Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
NoteText = Annotated[str, StringConstraints(min_length=1, max_length=100_000), AfterValidator(reject_blank)]


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal["text"]
    title: Title
    text: NoteText


class NoteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, strict=True)
    title: Title | None = None
    extracted_text: NoteText | None = None

    @model_validator(mode="after")
    def require_changes(self) -> Self:
        fields = self.model_fields_set - {"expected_version"}
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError("Provide a non-null title or extracted_text")
        return self


class SourceDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: Literal["text"]
    original_text: str


class NoteSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    source_type: Literal["text"] = "text"
    ingestion_status: Literal["ready"] = "ready"
    enrichment_status: Literal["not_requested"] = "not_requested"
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def restore_utc(cls, value: datetime) -> datetime:
        # SQLite does not retain timezone information in DATETIME columns.
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class NoteDetail(NoteSummary):
    source: SourceDetail
    extracted_text: str
    content_version: int


class NoteList(BaseModel):
    items: list[NoteSummary]
    total: int
    limit: int
    offset: int
