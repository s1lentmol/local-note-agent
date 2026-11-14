"""Target PostgreSQL jobs. All writers lock note before job.

Publication callbacks may execute SQL only: no model/file/network work. They
must not commit, alter the job, or delete its note (cleanup retains a tombstone).
"""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.postgres import Database


class StaleJob(Exception):
    """The occupation is no longer authorized to write."""


@dataclass(frozen=True)
class Job:
    id: UUID
    note_id: UUID
    kind: str
    lease_token: UUID
    attempts: int


Publication = Callable[[Connection, Job, Any], None]


class JobsRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _note(conn, note_id):
        return conn.execute(
            "SELECT id, deleted_at FROM notes WHERE id=%s FOR UPDATE", (note_id,)
        ).fetchone()

    @staticmethod
    def _eligible(note, kind):
        return note is not None and ((note["deleted_at"] is not None) == (kind == "cleanup"))

    def claim(self, lease_seconds: float) -> Job | None:
        """Select without job locks, then lock note -> queued job with SKIP LOCKED.

        Keeping the order identical to PATCH/DELETE avoids note/job deadlocks.
        Expired occupations are recovered explicitly before claiming.
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self.database.transaction() as conn:
            row = conn.execute(
                """SELECT n.id FROM notes n
                   WHERE EXISTS (SELECT 1 FROM jobs j WHERE j.note_id=n.id
                     AND j.status='queued'
                     AND ((j.kind='cleanup') = (n.deleted_at IS NOT NULL)))
                   ORDER BY (SELECT min(created_at) FROM jobs j
                             WHERE j.note_id=n.id AND j.status='queued'), n.id
                   LIMIT 1 FOR UPDATE OF n SKIP LOCKED"""
            ).fetchone()
            if row is None:
                return None
            job = conn.execute(
                """SELECT j.* FROM jobs j JOIN notes n ON n.id=j.note_id
                   WHERE j.note_id=%s AND j.status='queued'
                     AND ((j.kind='cleanup') = (n.deleted_at IS NOT NULL))
                   ORDER BY j.created_at, j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED""",
                (row["id"],),
            ).fetchone()
            if job is None:
                return None
            token = uuid4()
            conn.execute(
                """UPDATE jobs SET status='processing', attempts=attempts+1,
                   started_at=clock_timestamp(), finished_at=NULL, error=NULL,
                   lease_token=%s, lease_until=clock_timestamp()+%s WHERE id=%s""",
                (token, timedelta(seconds=lease_seconds), job["id"]),
            )
            if job["kind"] == "process":
                conn.execute(
                    """UPDATE notes SET processing_status='processing', processing_error=NULL,
                       updated_at=clock_timestamp() WHERE id=%s""",
                    (job["note_id"],),
                )
            if job["kind"] == "extract":
                conn.execute(
                    """UPDATE notes SET ingestion_status='processing', ingestion_error=NULL,
                       updated_at=clock_timestamp() WHERE id=%s""",
                    (job["note_id"],),
                )
            return Job(job["id"], job["note_id"], job["kind"], token, job["attempts"] + 1)

    @contextmanager
    def _active(self, job: Job):
        with self.database.transaction() as conn:
            note = self._note(conn, job.note_id)
            # Check the clock AFTER acquiring both locks, never transaction-start now().
            row = conn.execute("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (job.id,)).fetchone()
            if (
                not self._eligible(note, job.kind)
                or row is None
                or row["note_id"] != job.note_id
                or row["kind"] != job.kind
                or row["status"] != "processing"
                or row["lease_token"] != job.lease_token
                or not conn.execute(
                    "SELECT %s > clock_timestamp() AS live", (row["lease_until"],)
                ).fetchone()["live"]
            ):
                raise StaleJob()
            yield conn
            # A callback taking longer than the lease must roll back its writes too.
            if not conn.execute(
                "SELECT %s > clock_timestamp() AS live",
                (row["lease_until"],),
            ).fetchone()["live"]:
                raise StaleJob()

    def publish(self, job: Job, writer: Publication, payload: Any, *, complete=False):
        """The only result publication boundary, including intermediate stages."""
        with self._active(job) as conn:
            writer(conn, job, payload)
            if complete:
                self._finish(conn, job, "done", None)

    @staticmethod
    def _finish(conn, job, status, error):
        conn.execute(
            """UPDATE jobs SET status=%s, error=%s, finished_at=clock_timestamp(),
               lease_until=NULL WHERE id=%s""",
            (status, Jsonb(error) if error else None, job.id),
        )

        if job.kind == "process":
            conn.execute(
                """UPDATE notes SET processing_status=%s, processing_error=%s,
                   updated_at=clock_timestamp() WHERE id=%s""",
                (
                    "ready" if status == "done" else "failed",
                    Jsonb(error) if error else None,
                    job.note_id,
                ),
            )

        if job.kind == "extract" and status == "failed":
            conn.execute(
                """UPDATE notes SET ingestion_status='failed', ingestion_error=%s,
                   updated_at=clock_timestamp() WHERE id=%s""",
                (Jsonb(error), job.note_id),
            )

    def complete(self, job: Job):
        with self._active(job) as conn:
            self._finish(conn, job, "done", None)

    def fail(self, job: Job, code: str, message: str):
        with self._active(job) as conn:
            self._finish(conn, job, "failed", {"code": code, "message": message})

    def heartbeat(self, job: Job, lease_seconds: float):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._active(job) as conn:
            conn.execute(
                "UPDATE jobs SET lease_until=clock_timestamp()+%s WHERE id=%s",
                (timedelta(seconds=lease_seconds), job.id),
            )

    def recover_expired(self) -> int:
        """Crash recovery, not retry of failed jobs. Each note uses a short transaction."""
        with self.database.connection() as conn:
            candidates = conn.execute(
                """SELECT id, note_id FROM jobs WHERE status='processing'
                   AND lease_until <= clock_timestamp() ORDER BY note_id, id"""
            ).fetchall()
        count = 0
        for candidate in candidates:
            with self.database.transaction() as conn:
                note = self._note(conn, candidate["note_id"])
                row = conn.execute(
                    """SELECT * FROM jobs WHERE id=%s AND status='processing'
                       AND lease_until <= clock_timestamp() FOR UPDATE""",
                    (candidate["id"],),
                ).fetchone()
                if row is None:
                    continue
                eligible = self._eligible(note, row["kind"])
                conn.execute(
                    """UPDATE jobs SET status=%s, lease_token=NULL, lease_until=NULL,
                       started_at=NULL, finished_at=CASE WHEN %s THEN NULL
                       ELSE clock_timestamp() END WHERE id=%s""",
                    ("queued" if eligible else "cancelled", eligible, row["id"]),
                )
                if eligible and row["kind"] == "process":
                    conn.execute(
                        "UPDATE notes SET processing_status='queued' WHERE id=%s",
                        (row["note_id"],),
                    )
                if eligible and row["kind"] == "extract":
                    conn.execute(
                        "UPDATE notes SET ingestion_status='queued' WHERE id=%s",
                        (row["note_id"],),
                    )
                count += 1
        return count

    @staticmethod
    def cancel_active(conn: Connection, note_id: UUID) -> None:
        """Cancel all active jobs while the caller holds the note lock."""
        conn.execute(
            """UPDATE jobs SET status='cancelled', finished_at=clock_timestamp(),
               lease_token=NULL, lease_until=NULL
               WHERE note_id=%s AND status IN ('queued','processing')""",
            (note_id,),
        )

    @staticmethod
    def enqueue_cleanup(conn: Connection, note_id: UUID) -> None:
        """Queue the first cleanup while the caller holds the note lock."""
        conn.execute("INSERT INTO jobs (note_id, kind) VALUES (%s, 'cleanup')", (note_id,))

    def retry_latest(self, conn: Connection, note_id: UUID, kind: str) -> bool:
        """Retry the latest job only, under the caller's note lock."""
        row = conn.execute(
            """SELECT id FROM jobs WHERE note_id=%s AND kind=%s
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (note_id, kind),
        ).fetchone()
        return row is not None and self.retry(row["id"], conn=conn)

    def retry(self, job_id: UUID, *, conn: Connection | None = None) -> bool:
        """Explicit failed -> queued transition; active sibling means no retry."""
        if conn is None:
            with self.database.transaction() as transaction:
                return self.retry(job_id, conn=transaction)
        return self._retry(conn, job_id)

    def _retry(self, conn, job_id):
        candidate = conn.execute("SELECT note_id FROM jobs WHERE id=%s", (job_id,)).fetchone()
        if candidate is None:
            return False
        note = self._note(conn, candidate["note_id"])
        row = conn.execute("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
        if row is None or row["status"] != "failed" or not self._eligible(note, row["kind"]):
            return False
        if conn.execute(
            """SELECT 1 FROM jobs WHERE note_id=%s AND kind=%s
               AND (status IN ('queued','processing') OR (kind='cleanup' AND status='done'))""",
            (row["note_id"], row["kind"]),
        ).fetchone():
            return False
        conn.execute(
            """UPDATE jobs SET status='queued', error=NULL, started_at=NULL,
               finished_at=NULL, lease_until=NULL, lease_token=NULL WHERE id=%s""",
            (job_id,),
        )
        if row["kind"] == "process":
            conn.execute(
                """UPDATE notes SET processing_status='queued', processing_error=NULL,
                   updated_at=clock_timestamp() WHERE id=%s""",
                (row["note_id"],),
            )
        if row["kind"] == "extract":
            conn.execute(
                """UPDATE notes SET ingestion_status='queued', ingestion_error=NULL,
                   updated_at=clock_timestamp() WHERE id=%s""",
                (row["note_id"],),
            )
        return True
