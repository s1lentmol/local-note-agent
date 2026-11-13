import os

from pydantic import BaseModel, SecretStr


class BotSettings(BaseModel):
    token: SecretStr

    @classmethod
    def from_env(cls):
        token = os.environ.get("NOTE_AGENT_TELEGRAM_TOKEN", "").strip()
        if not token:
            raise ValueError("NOTE_AGENT_TELEGRAM_TOKEN is required")
        return cls(token=SecretStr(token))
