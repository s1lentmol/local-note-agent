"""Remove the stored originals before publishing cleanup through the worker."""

from dataclasses import dataclass
from pathlib import Path

import psycopg

from app.repositories.cleanup import CleanupRepository
from app.services.storage import Storage


@dataclass(frozen=True)
class CleanupHandler:
    database_url: str
    data_dir: Path

    def __call__(self, job, context):
        with psycopg.connect(self.database_url, autocommit=True) as conn:
            keys = CleanupRepository.storage_keys(conn, job.note_id)
        if keys:
            storage = Storage(self.data_dir)
            for key in keys:
                storage.remove(key)
        context.publish(None)
