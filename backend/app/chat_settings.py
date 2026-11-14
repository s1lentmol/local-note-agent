import os

from pydantic import BaseModel, Field, model_validator


class ChatSettings(BaseModel):
    context_tokens: int = Field(default=8192, ge=2048, le=131072)
    response_tokens: int = Field(default=512, ge=1, le=8192)
    max_response_bytes: int = Field(default=3600, ge=128, le=4096)
    max_steps: int = Field(default=4, ge=1, le=16)
    max_tool_calls: int = Field(default=6, ge=1, le=32)
    history_messages: int = Field(default=40, ge=1, le=256)
    tool_result_bytes: int = Field(default=1800, ge=256, le=16384)
    search_limit: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def check_budget(self):
        if self.response_tokens + 1536 >= self.context_tokens:
            raise ValueError("Chat context must fit the response and tool definitions")
        return self

    @classmethod
    def from_env(cls):
        return cls.model_validate(
            {
                name: os.environ["NOTE_AGENT_CHAT_" + name.upper()]
                for name in cls.model_fields
                if "NOTE_AGENT_CHAT_" + name.upper() in os.environ
            }
        )
