import math
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerSettings:
    lease_seconds: float = 30
    heartbeat_seconds: float = 5
    timeout_seconds: float = 600
    poll_seconds: float = 1
    stop_grace_seconds: float = 1

    def __post_init__(self):
        for value in vars(self).values():
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Worker intervals must be finite and positive")
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("Heartbeat interval must be shorter than lease")

    @classmethod
    def from_env(cls):
        return cls(
            **{
                name: float(os.environ.get("NOTE_AGENT_PG_WORKER_" + name.upper(), default))
                for name, default in vars(cls()).items()
            }
        )
