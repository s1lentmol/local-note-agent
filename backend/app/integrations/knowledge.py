import importlib
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.errors import ProcessingError


class NoteDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_id: str
    content_version: int = Field(ge=1)
    title: str
    text: str


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=100)


class EnrichmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_id: str
    content_version: int = Field(ge=1)
    summary: str = Field(max_length=100_000)
    tags: list[str] = Field(default_factory=list, max_length=100)
    category: str | None = Field(default=None, max_length=200)
    entities: list[Entity] = Field(default_factory=list, max_length=1000)


class KnowledgeProcessor(Protocol):
    def upsert_note(self, document: NoteDocument) -> EnrichmentResult: ...
    def delete_note(self, note_id: str) -> None: ...


def load_adapter(settings) -> KnowledgeProcessor:
    """NOTE_AGENT_KNOWLEDGE_ADAPTER=package.module:factory; factory(settings)."""
    if not settings.knowledge_adapter:
        raise ProcessingError("enrichment_disabled", "Модуль обработки знаний не подключён.")
    try:
        module, factory = settings.knowledge_adapter.split(":", 1)
        adapter = getattr(importlib.import_module(module), factory)(settings)
        if not callable(getattr(adapter, "upsert_note", None)) or not callable(
            getattr(adapter, "delete_note", None)
        ):
            raise TypeError("Invalid adapter")
        return adapter
    except Exception as exc:
        raise ProcessingError(
            "adapter_unavailable", "Не удалось загрузить адаптер обработки знаний."
        ) from exc
