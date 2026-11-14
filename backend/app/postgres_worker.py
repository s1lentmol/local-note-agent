"""Independent entry point. Does not load prototype services or migrate schema."""

import argparse
import importlib
import os
import signal

from app.job_runner.settings import WorkerSettings
from app.job_runner.worker import Worker
from app.jobs_repository import JobsRepository
from app.postgres import Database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry", type=str, help="Explicitly retry a failed job UUID, then exit")
    args = parser.parse_args()
    # The default factory registers extract, process and cleanup.
    factory_path = os.environ.get("NOTE_AGENT_PG_WORKER_HANDLERS", "app.indexing:handlers")
    handlers = {}
    if factory_path:
        module, name = factory_path.split(":", 1)
        handlers = getattr(importlib.import_module(module), name)()
    settings = WorkerSettings.from_env()
    with Database(os.environ["NOTE_AGENT_DATABASE_URL"]) as database:
        repository = JobsRepository(database)
        if args.retry:
            from uuid import UUID

            if not repository.retry(UUID(args.retry)):
                parser.exit(1, "Job is not eligible for retry\n")
            return
        worker = Worker(repository, handlers, settings)
        previous = {}
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, lambda *_: worker.stop())
            worker.run()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    main()
