"""One spawned computation at a time; parent owns every database transaction.

Handlers must be spawn-picklable callables (job, context). They compute outside
transactions and context.publish(payload) requests a short parent-side SQL write.
Register a parent-side writer for each handler that publishes intermediate data.
Handlers return only after all their stages succeed; returning completes the job.
No Database, pool or connection is passed to the child. Payloads are trusted local
Python objects. SQL writers must obey JobsRepository.publish's contract.
"""

import multiprocessing
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any

from app.errors import ProcessingError
from app.extractors.text import TEXT_ERRORS
from app.job_runner.settings import WorkerSettings
from app.jobs_repository import Job, JobsRepository, Publication, StaleJob


class HandlerContext:
    def __init__(self, channel: Connection):
        self.channel = channel

    def publish(self, payload: Any):
        self.channel.send(("publish", payload))
        if self.channel.recv() != "ok":
            raise StaleJob()


Handler = Callable[[Job, HandlerContext], None]


@dataclass(frozen=True)
class HandlerBinding:
    compute: Handler
    writer: Publication | None = None
    complete_on_publish: bool = False


def _compute(handler, job, channel):
    try:
        handler(job, HandlerContext(channel))
        channel.send(("done", None))
    except ProcessingError as exc:
        # Fixed allowlist only: exception messages and arbitrary codes never escape.
        code = exc.code if job.kind == "extract" and exc.code in TEXT_ERRORS else None
        channel.send(("error", code))
    except BaseException:
        # Do not persist handler exceptions, inputs, credentials or provider bodies.
        channel.send(("error", None))
    finally:
        channel.close()


class Worker:
    def __init__(
        self,
        repository: JobsRepository,
        handlers: Mapping[str, HandlerBinding],
        settings: WorkerSettings | None = None,
    ):
        if set(handlers) - {"extract", "process", "cleanup"}:
            raise ValueError("Unknown job kind")
        self.repository = repository
        self.handlers = dict(handlers)
        self.settings = settings or WorkerSettings()
        self.stopping = threading.Event()
        self.context = multiprocessing.get_context("spawn")

    def stop(self):
        """Signal-safe request; run loop interrupts current computation and joins it."""
        self.stopping.set()

    def run(self):
        while not self.stopping.is_set():
            if not self.run_once():
                self.stopping.wait(self.settings.poll_seconds)

    def _fail(self, job, code, message):
        try:
            self.repository.fail(job, code, message)
        except StaleJob:
            pass

    def run_once(self) -> bool:
        if self.stopping.is_set():
            return False
        self.repository.recover_expired()
        job = self.repository.claim(self.settings.lease_seconds)
        if job is None:
            return False
        binding = self.handlers.get(job.kind)
        if binding is None:
            self._fail(job, "handler_missing", "No handler configured for this job kind")
            return True
        parent, child = self.context.Pipe()
        process = self.context.Process(target=_compute, args=(binding.compute, job, child))
        started = False
        completed = False
        deadline = time.monotonic() + self.settings.timeout_seconds
        heartbeat_at = time.monotonic() + self.settings.heartbeat_seconds
        try:
            process.start()
            started = True
            child.close()
            while True:
                now = time.monotonic()
                if self.stopping.is_set():
                    self._fail(job, "worker_stopped", "Worker stopped during processing")
                    break
                if now >= deadline:
                    self._fail(job, "job_timeout", "Job processing deadline exceeded")
                    break
                if now >= heartbeat_at:
                    self.repository.heartbeat(job, self.settings.lease_seconds)
                    heartbeat_at = time.monotonic() + self.settings.heartbeat_seconds
                wait = min(deadline - now, max(0, heartbeat_at - now), 0.05)
                if parent.poll(wait):
                    event, payload = parent.recv()
                    if self.stopping.is_set():
                        self._fail(job, "worker_stopped", "Worker stopped during processing")
                        break
                    if time.monotonic() >= deadline:
                        self._fail(job, "job_timeout", "Job processing deadline exceeded")
                        break
                    if event == "publish":
                        if binding.writer is None:
                            raise RuntimeError("No publication writer")
                        self.repository.publish(
                            job, binding.writer, payload, complete=binding.complete_on_publish
                        )
                        completed = binding.complete_on_publish
                        parent.send("ok")
                    elif event == "done":
                        if not completed:
                            self.repository.complete(job)
                        break
                    elif event == "error" and job.kind == "extract" and payload in TEXT_ERRORS:
                        self._fail(job, payload, TEXT_ERRORS[payload])
                        break
                    else:
                        self._fail(job, "handler_failed", "Job handler failed")
                        break
                elif not process.is_alive():
                    self._fail(job, "handler_failed", "Job handler exited without a result")
                    break
        except StaleJob:
            # Lost lease or cancelled note: stop computation, never finish another token.
            pass
        except Exception:
            self._fail(job, "handler_failed", "Job handler or publication failed")
        finally:
            child.close()
            parent.close()
            if started:
                if process.is_alive():
                    process.terminate()
                process.join(self.settings.stop_grace_seconds)
                if process.is_alive():
                    process.kill()
                    process.join(self.settings.stop_grace_seconds)
                process.close()
        return True
