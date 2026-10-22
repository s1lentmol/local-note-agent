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
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "source_type": "text", "title": "Конспект", "text": "Текст заметки",
    }]})

    source_type: Literal["text"] = Field(description="Тип источника. Сейчас поддерживается только текст.")
    title: Title = Field(description="Заголовок заметки, до 200 символов.")
    text: NoteText = Field(description="Исходный текст, до 100 000 символов. Не может состоять только из пробелов.")


class NoteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "expected_version": 1, "title": "Обновлённый конспект", "extracted_text": "Исправленный текст",
    }]})

    expected_version: int = Field(ge=1, strict=True, description="Текущее значение content_version полученной заметки.")
    title: Title | None = Field(default=None, description="Новый заголовок. Поле можно пропустить, но нельзя передать null.")
    extracted_text: NoteText | None = Field(default=None, description="Новый текст. Поле можно пропустить, но нельзя передать null.")

    @model_validator(mode="after")
    def require_changes(self) -> Self:
        fields = self.model_fields_set - {"expected_version"}
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError("Provide a non-null title or extracted_text")
        return self


class SourceDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: Literal["text"]
    original_text: str = Field(description="Исходный текст при создании. Не меняется при редактировании заметки.")


class NoteSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    source_type: Literal["text"] = "text"
    ingestion_status: Literal["ready"] = Field(default="ready", description="Текст доступен сразу после создания.")
    enrichment_status: Literal["not_requested"] = Field(default="not_requested", description="Обработка ИИ пока не подключена.")
    created_at: datetime = Field(description="Время создания в UTC.")
    updated_at: datetime = Field(description="Время последнего изменения в UTC.")

    @field_validator("created_at", "updated_at")
    @classmethod
    def restore_utc(cls, value: datetime) -> datetime:
        # SQLite does not retain timezone information in DATETIME columns.
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class NoteDetail(NoteSummary):
    source: SourceDetail
    extracted_text: str = Field(description="Текущий редактируемый текст заметки.")
    content_version: int = Field(description="Версия заголовка и текста. Увеличивается при изменении содержимого.")


class NoteList(BaseModel):
    items: list[NoteSummary]
    total: int = Field(description="Общее число заметок, независимо от размера страницы.")
    limit: int = Field(description="Максимальное число элементов на странице.")
    offset: int = Field(description="Количество пропущенных элементов.")


class ErrorDetail(BaseModel):
    code: str = Field(description="Код ошибки для обработки клиентом.")
    message: str = Field(description="Описание ошибки.")
    fields: list[list[str | int]] | None = Field(default=None, description="Пути к полям с ошибками валидации, если применимо.")


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: Literal["ok"]
