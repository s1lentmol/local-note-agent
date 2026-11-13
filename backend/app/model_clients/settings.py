"""Explicit, independent environment settings; no app.config dependency."""

import json
import os
from typing import Any

from pydantic import Field, SecretStr, field_validator

from .contracts import StrictModel


class EndpointSettings(StrictModel):
    base_url: str
    api_key: SecretStr = SecretStr("")
    timeout: float = Field(default=180.0, gt=0)

    @field_validator("base_url")
    @classmethod
    def url(cls, value):
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Expected HTTP endpoint without credentials/query/fragment")
        return value.rstrip("/")

    @classmethod
    def from_env(cls):
        prefix = {"LLMSettings": "LLM_", "EmbeddingSettings": "EMBEDDING_", "ASRSettings": "ASR_"}[
            cls.__name__
        ]
        values = {}
        for name in cls.model_fields:
            raw = os.environ.get(prefix + name.upper())
            if raw is not None:
                if name == "options":
                    values[name] = json.loads(raw)
                elif name in {"timeout", "norm_tolerance"}:
                    values[name] = float(raw)
                elif name in {"dimensions", "num_ctx", "max_tokens", "max_bytes"}:
                    values[name] = int(raw)
                else:
                    values[name] = raw
        return cls(**values)


class LLMSettings(EndpointSettings):
    base_url: str = "http://127.0.0.1:11435/v1"
    model: str = Field(default="qwen3:8b", min_length=1)
    max_tokens: int = Field(default=512, gt=0)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("options")
    @classmethod
    def reserved(cls, value):
        if set(value) & {"model", "messages", "stream", "tools", "response_format", "max_tokens"}:
            raise ValueError("Profile options cannot override protocol fields")
        return value


class EmbeddingSettings(EndpointSettings):
    base_url: str = "http://127.0.0.1:11435"
    model: str = Field(default="qwen3-embedding:0.6b", min_length=1)
    digest: str = Field(
        default="ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d",
        pattern=r"^[a-f0-9]{64}$",
    )
    dimensions: int = Field(default=1024, gt=0)
    num_ctx: int = Field(default=8192, gt=0)
    query_instruction: str = (
        "Given a web search query, retrieve relevant passages that answer the query"
    )
    norm_tolerance: float = Field(default=0.01, gt=0, lt=1)


class ASRSettings(EndpointSettings):
    base_url: str = "http://127.0.0.1:18082"
    timeout: float = Field(default=600.0, gt=0)
    max_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
