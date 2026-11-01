import fcntl
import logging
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC

from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import create_sqlite_engine
from app.migrate import migrate
from app.models import Job, Note, WorkerState, utc_now
from app.processing import finish_error, is_current, process_job

logger = logging.getLogger(__name__)


@contextmanager
def worker_lock(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with (settings.data_dir / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another worker is already running for this database.") from exc
        yield lock


def capabilities(settings):
    available = {"ocr": False, "audio": False, "knowledge": False}
    try:
        languages = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True, check=True, timeout=5
        ).stdout.splitlines()
        available["ocr"] = set(settings.ocr_language.split("+")) <= set(languages)
    except Exception:
        pass
    try:
        from faster_whisper.utils import download_model

        download_model(
            settings.whisper_model, cache_dir=str(settings.model_dir), local_files_only=True
        )
        available["audio"] = True
    except Exception:
        pass
    if settings.knowledge_adapter:
        try:
            from app.integrations.knowledge import load_adapter

            load_adapter(settings)
            available["knowledge"] = True
        except Exception:
            pass
    return available


class Worker:
    def __init__(self, settings, database_path=None):
        self.settings = settings
        self.engine = create_sqlite_engine(database_path or settings.database_path)
        migrate(self.engine)
        self.factory = sessionmaker(self.engine)
        self.stopping = False
        self.available = {}

    def heartbeat(self):
        with self.factory() as session:
            session.merge(WorkerState(id=1, heartbeat_at=utc_now(), capabilities=self.available))
            session.commit()

    def recover(self):
        with self.factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            for job in session.scalars(select(Job).where(Job.status == "processing")):
                note = session.get(Note, job.note_id)
                if not is_current(job, note):
                    job.status = "cancelled"
                    job.finished_at = utc_now()
                else:
                    job.status = "queued"
                    job.started_at = None
                    if job.kind != "cleanup":
                        setattr(
                            note,
                            ("ingestion" if job.kind == "extract" else "enrichment") + "_status",
                            "queued",
                        )
            session.commit()

    def claim(self):
        with self.factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            job = session.scalar(
                select(Job).where(Job.status == "queued").order_by(Job.created_at, Job.id).limit(1)
            )
            if job is None:
                return None
            note = session.get(Note, job.note_id)
            if not is_current(job, note):
                job.status = "cancelled"
                job.finished_at = utc_now()
                session.commit()
                return None
            job.status = "processing"
            job.attempts += 1
            job.started_at = utc_now()
            if job.kind != "cleanup":
                setattr(
                    note,
                    ("ingestion" if job.kind == "extract" else "enrichment") + "_status",
                    "processing",
                )
            session.commit()
            return job.id

    def run_once(self, adapter=None):
        """Synchronous execution for deterministic integration tests; CLI uses isolated processes."""
        job_id = self.claim()
        if job_id:
            process_job(self.factory, self.settings, job_id, adapter=adapter)
        return job_id

    def execute_isolated(self, job_id, lock):
        environment = os.environ.copy()
        environment["NOTE_AGENT_SETTINGS_JSON"] = self.settings.model_dump_json()
        process = subprocess.Popen(
            [sys.executable, "-m", "app.worker", "--job", job_id],
            env=environment,
            start_new_session=True,
            pass_fds=(lock.fileno(),),
        )
        started = time.monotonic()
        try:
            while process.poll() is None:
                self.heartbeat()
                if self.stopping or time.monotonic() - started > self.settings.job_timeout_seconds:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    code = "worker_stopped" if self.stopping else "processing_timeout"
                    finish_error(
                        self.factory,
                        job_id,
                        {
                            "code": code,
                            "message": "Обработка прервана или превысила допустимое время. Повторите попытку.",
                        },
                    )
                    return
                time.sleep(0.5)
            if process.returncode:
                finish_error(
                    self.factory,
                    job_id,
                    {
                        "code": "worker_process_failed",
                        "message": "Процесс обработки завершился с ошибкой. Повторите попытку.",
                    },
                )
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    def close(self):
        self.engine.dispose()

    def run(self):
        with worker_lock(self.settings) as lock:
            self.recover()
            self.available = capabilities(self.settings)

            def stop(signum, frame):
                self.stopping = True

            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            try:
                while not self.stopping:
                    self.heartbeat()
                    job_id = self.claim()
                    if job_id:
                        self.execute_isolated(job_id, lock)
                    else:
                        time.sleep(1)
            finally:
                with self.factory() as session:
                    state = session.get(WorkerState, 1)
                    if state:
                        session.delete(state)
                        session.commit()
                self.close()


def main():
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) == 3 and sys.argv[1] == "--job":
        settings = Settings.model_validate_json(os.environ["NOTE_AGENT_SETTINGS_JSON"])
        engine = create_sqlite_engine(settings.database_path)
        try:
            process_job(sessionmaker(engine), settings, sys.argv[2])
        finally:
            engine.dispose()
    elif "--check" in sys.argv:
        settings = Settings.from_env()
        engine = create_sqlite_engine(settings.database_path)
        try:
            with sessionmaker(engine)() as session:
                state = session.get(WorkerState, 1)
                healthy = (
                    state
                    and (utc_now() - state.heartbeat_at.replace(tzinfo=UTC)).total_seconds() < 15
                )
                raise SystemExit(0 if healthy else 1)
        finally:
            engine.dispose()
    else:
        Worker(Settings.from_env()).run()


if __name__ == "__main__":
    main()
