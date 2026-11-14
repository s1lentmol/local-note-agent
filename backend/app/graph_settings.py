"""Settings for automatic semantic links at index publication."""

import os

from pydantic import BaseModel, Field


class GraphSettings(BaseModel, frozen=True):
    top_k: int = Field(default=5, ge=1, le=100)
    min_score: float = Field(default=0.75, ge=-1, le=1, allow_inf_nan=False)

    @classmethod
    def from_env(cls):
        return cls(
            **{
                name: os.environ["NOTE_AGENT_GRAPH_" + name.upper()]
                for name in cls.model_fields
                if "NOTE_AGENT_GRAPH_" + name.upper() in os.environ
            }
        )
