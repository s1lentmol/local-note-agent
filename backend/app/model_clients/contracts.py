"""Model boundaries: no persistence or application tool execution."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

Result = TypeVar("Result", bound=BaseModel)


class ModelClientError(RuntimeError):
    """Safe message only: never include upstream bodies, URLs or input data."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ToolCall(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class Message(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class Generation(StrictModel):
    content: str | None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str

    def as_message(self) -> Message:
        return Message(role="assistant", content=self.content, tool_calls=self.tool_calls)


class Tool:
    def __init__(self, name: str, description: str, arguments: type[BaseModel]):
        self.name, self.description, self.arguments = name, description, arguments


class Transcript(StrictModel):
    text: str = Field(min_length=1)
    language: str = Field(pattern=r"^[a-z]{2,3}$")


class TextGenerator(Protocol):
    def chat(self, messages: list[Message], tools: Sequence[Tool] = ()) -> Generation: ...
    def json(self, messages: list[Message], schema: type[Result]) -> Result: ...


class Embedder(Protocol):
    @property
    def identity(self) -> str: ...
    @property
    def dimensions(self) -> int: ...
    def documents(self, texts: list[str]) -> list[list[float]]: ...
    def queries(self, texts: list[str]) -> list[list[float]]: ...


class Transcriber(Protocol):
    def transcribe(self, audio: Path) -> Transcript: ...
