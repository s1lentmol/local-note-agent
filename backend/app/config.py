import os
from pathlib import Path

from pydantic import BaseModel, Field


class Settings(BaseModel):
    data_dir: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[1] / "data")
    image_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    audio_max_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    html_max_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    text_max_chars: int = Field(default=100_000, gt=0, le=100_000)
    audio_max_seconds: int = Field(default=600, gt=0)
    image_max_pixels: int = Field(default=25_000_000, gt=0)
    job_timeout_seconds: int = Field(default=900, gt=0)
    web_timeout_seconds: int = Field(default=20, gt=0)
    ocr_language: str = "rus+eng"
    whisper_model: str = "base"
    knowledge_adapter: str = ""
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    @property
    def database_path(self) -> Path:
        return self.data_dir / "notes.sqlite3"

    @property
    def model_dir(self) -> Path:
        return self.data_dir / "models"

    @classmethod
    def from_env(cls):
        values = {}
        for name in cls.model_fields:
            value = os.environ.get("NOTE_AGENT_" + name.upper())
            if value is not None:
                values[name] = (
                    [item.strip() for item in value.split(",") if item.strip()]
                    if name == "cors_origins"
                    else value
                )
        return cls(**values)
