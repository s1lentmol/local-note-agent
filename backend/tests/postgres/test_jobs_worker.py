"""Real PostgreSQL/pgvector only; each test gets T02's disposable schema."""

import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from uuid import uuid4

import pytest
from psycopg.conninfo import make_conninfo

from app.job_runner.settings import WorkerSettings
from app.job_runner.worker import HandlerBinding, Worker
from app.jobs_repository import JobsRepository, StaleJob
from app.postgres import Database
from migrate import migrate


@pytest.fixture
def repository(database_url):
    migrate(database_url)
    with Database(database_url) as database:
        yield JobsRepository(database)


def create(repository, kind="process", text="ok", deleted=False):
    with repository.database.transaction() as conn:
        note = conn.execute(
            """INSERT INTO notes (title, extracted_text, ingestion_status, deleted_at)
               VALUES ('T06 synthetic', %s, 'ready', CASE WHEN %s THEN clock_timestamp() END)
               RETURNING id""",
            (text, deleted),
        ).fetchone()["id"]
        job = conn.execute(
            "INSERT INTO jobs (note_id, kind) VALUES (%s,%s) RETURNING id", (note, kind)
        ).fetchone()["id"]
    return note, job


def state(repository, job_id):
    with repository.database.connection() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()


def expire(repository, job):
    # Controlled passage of time on the DB clock, no long sleep or clock mocking.
    with repository.database.transaction() as conn:
        conn.execute("SELECT id FROM notes WHERE id=%s FOR UPDATE", (job.note_id,))
        conn.execute(
            "UPDATE jobs SET lease_until=clock_timestamp()-interval '1 millisecond' WHERE id=%s",
            (job.id,),
        )


def cancel(repository, job):
    with repository.database.transaction() as conn:
        conn.execute("SELECT id FROM notes WHERE id=%s FOR UPDATE", (job.note_id,))
        conn.execute(
            """UPDATE jobs SET status='cancelled', finished_at=clock_timestamp()
               WHERE id=%s""",
            (job.id,),
        )


def write_summary(conn, job, value):
    conn.execute("UPDATE notes SET summary=%s WHERE id=%s", (value, job.note_id))


def read_summary(repository, note):
    with repository.database.connection() as conn:
        return conn.execute("SELECT summary FROM notes WHERE id=%s", (note,)).fetchone()["summary"]


def success(job, context):
    context.publish("published")


def fail_handler(job, context):
    raise RuntimeError("private provider body must never reach jobs.error")


def hang(job, context):
    time.sleep(30)


def ignore_termination(job, context):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    context.publish("running")
    time.sleep(30)


def cli_handlers():
    return {"process": HandlerBinding(ignore_termination, write_summary)}


def crash(job, context):
    os._exit(7)


def staged_failure(job, context):
    context.publish("index retained")
    raise RuntimeError("enrichment failed")


def slow_success(job, context):
    time.sleep(0.65)
    context.publish("published")


def settings(**kwargs):
    return replace(
        WorkerSettings(
            lease_seconds=1,
            heartbeat_seconds=0.1,
            timeout_seconds=3,
            poll_seconds=0.02,
            stop_grace_seconds=0.2,
        ),
        **kwargs,
    )


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("condition not reached before deadline")


def test_claim_lease_token_recovery_and_stale_writes(repository):
    note, job_id = create(repository)
    first = repository.claim(1)
    assert first.id == job_id and first.attempts == 1
    assert repository.claim(1) is None
    assert repository.recover_expired() == 0
    before = state(repository, job_id)["lease_until"]
    repository.heartbeat(first, 2)
    assert state(repository, job_id)["lease_until"] > before
    expire(repository, first)
    for operation in (
        lambda: repository.publish(first, write_summary, "expired"),
        lambda: repository.complete(first),
        lambda: repository.fail(first, "bad", "bad"),
        lambda: repository.heartbeat(first, 1),
    ):
        with pytest.raises(StaleJob):
            operation()
    assert repository.recover_expired() == 1
    second = repository.claim(2)
    assert second.id == first.id and second.attempts == 2
    assert second.lease_token != first.lease_token
    for operation in (
        lambda: repository.publish(first, write_summary, "stale"),
        lambda: repository.complete(first),
        lambda: repository.fail(first, "bad", "bad"),
        lambda: repository.heartbeat(first, 1),
    ):
        with pytest.raises(StaleJob):
            operation()
    assert read_summary(repository, note) is None
    assert state(repository, job_id)["lease_token"] == second.lease_token
    repository.publish(second, write_summary, "current", complete=True)
    assert read_summary(repository, note) == "current"
    assert state(repository, job_id)["status"] == "done"


def test_cancelled_and_deleted_reject_publication_and_heartbeat(repository):
    note, job_id = create(repository)
    job = repository.claim(2)
    cancel(repository, job)
    before = state(repository, job_id)["lease_until"]
    with pytest.raises(StaleJob):
        repository.publish(job, write_summary, "cancelled", complete=True)
    with pytest.raises(StaleJob):
        repository.heartbeat(job, 5)
    assert state(repository, job_id)["lease_until"] == before
    assert read_summary(repository, note) is None
    # Even an active job cannot publish after tombstoning (defence in depth).
    note, _ = create(repository)
    job = repository.claim(2)
    with repository.database.transaction() as conn:
        conn.execute("UPDATE notes SET deleted_at=clock_timestamp() WHERE id=%s", (note,))
    with pytest.raises(StaleJob):
        repository.publish(job, write_summary, "deleted")
    expire(repository, job)
    assert repository.recover_expired() == 1
    assert state(repository, job.id)["status"] == "cancelled"


def test_fake_job_identity_cannot_write(repository):
    create(repository)
    job = repository.claim(2)
    for forged in (replace(job, lease_token=uuid4()), replace(job, kind="extract")):
        with pytest.raises(StaleJob):
            repository.publish(forged, write_summary, "forged")
        with pytest.raises(StaleJob):
            repository.heartbeat(forged, 1)


def test_callback_rollback_and_lease_expiring_during_publication(repository):
    note, _ = create(repository)
    job = repository.claim(0.15)

    def too_slow(conn, job, payload):
        write_summary(conn, job, payload)
        conn.execute("SELECT pg_sleep(0.2)")

    with pytest.raises(StaleJob):
        repository.publish(job, too_slow, "rolled back", complete=True)
    assert read_summary(repository, note) is None
    assert state(repository, job.id)["status"] == "processing"
    repository.recover_expired()
    job = repository.claim(2)

    def broken(conn, job, payload):
        write_summary(conn, job, payload)
        raise ValueError()

    with pytest.raises(ValueError):
        repository.publish(job, broken, "rollback")
    assert read_summary(repository, note) is None


def test_note_lock_skip_locked_and_job_lock_skip_locked(repository):
    note, job_id = create(repository)
    other_note, other_id = create(repository)
    with repository.database.transaction() as conn:
        conn.execute("SELECT id FROM notes WHERE id=%s FOR UPDATE", (note,))
        claimed = repository.claim(2)
        assert claimed.id == other_id
    repository.complete(claimed)
    with repository.database.transaction() as conn:
        conn.execute("SELECT id FROM jobs WHERE id=%s FOR UPDATE", (job_id,))
        assert repository.claim(2) is None
    assert repository.claim(2).note_id == note
    assert other_note != note


def test_publication_waiting_for_note_checks_fresh_clock(repository):
    note, _ = create(repository)
    job = repository.claim(0.15)
    errors = []

    def publish():
        try:
            repository.publish(job, write_summary, "late")
        except StaleJob:
            errors.append("stale")

    with repository.database.transaction() as conn:
        conn.execute("SELECT id FROM notes WHERE id=%s FOR UPDATE", (note,))
        thread = threading.Thread(target=publish)
        thread.start()
        conn.execute("SELECT pg_sleep(0.2)")
    thread.join(2)
    assert not thread.is_alive() and errors == ["stale"]
    assert read_summary(repository, note) is None


def test_manual_retry_and_active_sibling(repository):
    note, job_id = create(repository)
    job = repository.claim(2)
    repository.fail(job, "test_failure", "Synthetic failure")
    assert state(repository, job_id)["error"]["code"] == "test_failure"
    assert repository.claim(2) is None  # failed is never automatically retried
    with repository.database.transaction() as conn:
        sibling = conn.execute(
            "INSERT INTO jobs (note_id,kind) VALUES (%s,'process') RETURNING id", (note,)
        ).fetchone()["id"]
    assert not repository.retry(job_id)
    cancel(repository, repository.claim(2))
    assert state(repository, sibling)["status"] == "cancelled"
    assert repository.retry(job_id)
    assert not repository.retry(job_id)
    retried = repository.claim(2)
    assert retried.id == job_id and retried.attempts == 2
    assert retried.lease_token != job.lease_token
    repository.complete(retried)
    assert not repository.retry(job_id)
    assert not repository.retry(uuid4())


@pytest.mark.parametrize(
    "kind,deleted", [("extract", False), ("process", False), ("cleanup", True)]
)
def test_each_kind_and_cleanup_tombstone(repository, kind, deleted):
    note, job_id = create(repository, kind=kind, deleted=deleted)
    worker = Worker(repository, {kind: HandlerBinding(success, write_summary)}, settings())
    assert worker.run_once()
    assert state(repository, job_id)["status"] == "done"
    assert read_summary(repository, note) == "published"
    assert not worker.run_once()


@pytest.mark.parametrize(
    "handler,code,intervals",
    [
        (fail_handler, "handler_failed", {}),
        (crash, "handler_failed", {}),
        (hang, "job_timeout", {"timeout_seconds": 0.6}),
    ],
)
def test_handler_failure_timeout_and_next_job(repository, handler, code, intervals):
    _, failed_id = create(repository)
    _, next_id = create(repository, kind="extract")
    worker = Worker(
        repository,
        {"process": HandlerBinding(handler), "extract": HandlerBinding(success, write_summary)},
        settings(**intervals),
    )
    assert worker.run_once()
    failed = state(repository, failed_id)
    assert failed["status"] == "failed" and failed["error"]["code"] == code
    assert "private" not in str(failed["error"])
    assert worker.run_once()
    assert state(repository, next_id)["status"] == "done"
    assert multiprocessing.active_children() == []


def test_missing_handler_and_missing_writer_fail(repository):
    _, job_id = create(repository)
    Worker(repository, {}, settings()).run_once()
    assert state(repository, job_id)["error"]["code"] == "handler_missing"
    assert repository.retry(job_id)
    Worker(repository, {"process": HandlerBinding(success)}, settings()).run_once()
    assert state(repository, job_id)["status"] == "failed"


def test_intermediate_publication_retained_on_handler_failure(repository):
    note, job_id = create(repository)
    Worker(
        repository, {"process": HandlerBinding(staged_failure, write_summary)}, settings()
    ).run_once()
    assert state(repository, job_id)["status"] == "failed"
    assert read_summary(repository, note) == "index retained"


def test_heartbeat_during_computation_and_sequential_loop(repository):
    note, job_id = create(repository)
    _, next_id = create(repository)
    worker = Worker(
        repository,
        {"process": HandlerBinding(slow_success, write_summary)},
        settings(lease_seconds=0.4),
    )
    thread = threading.Thread(target=worker.run)
    thread.start()
    try:
        wait_for(lambda: state(repository, job_id)["status"] == "processing")
        first_lease = state(repository, job_id)["lease_until"]
        wait_for(lambda: state(repository, job_id)["lease_until"] != first_lease)
        assert state(repository, next_id)["status"] == "queued"
        wait_for(lambda: state(repository, next_id)["status"] == "done")
        assert read_summary(repository, note) == "published"
        assert state(repository, job_id)["status"] == "done"
    finally:
        worker.stop()
        thread.join(3)
    assert not thread.is_alive()
    assert multiprocessing.active_children() == []


def test_stop_active_worker_and_no_claim_after_stop(repository):
    _, job_id = create(repository)
    _, next_id = create(repository)
    worker = Worker(repository, {"process": HandlerBinding(hang)}, settings())
    thread = threading.Thread(target=worker.run)
    thread.start()
    wait_for(lambda: state(repository, job_id)["status"] == "processing")
    worker.stop()
    thread.join(3)
    assert not thread.is_alive()
    assert state(repository, job_id)["error"]["code"] == "worker_stopped"
    assert state(repository, next_id)["status"] == "queued"
    assert not worker.run_once()
    assert multiprocessing.active_children() == []


def test_cancellation_interrupts_running_handler(repository):
    note, job_id = create(repository)
    worker = Worker(repository, {"process": HandlerBinding(hang)}, settings())
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    wait_for(lambda: state(repository, job_id)["status"] == "processing")
    row = state(repository, job_id)
    from app.jobs_repository import Job

    cancel(repository, Job(job_id, note, "process", row["lease_token"], row["attempts"]))
    thread.join(3)
    assert not thread.is_alive()
    assert state(repository, job_id)["status"] == "cancelled"
    assert multiprocessing.active_children() == []


@pytest.mark.parametrize("active", [False, True])
def test_entrypoint_retry_and_sigterm_closes_pool(repository, database_url, active):
    note, job_id = create(repository)
    job = repository.claim(2)
    repository.fail(job, "synthetic", "Synthetic")
    application_name = "t06_cli_" + uuid4().hex
    env = dict(
        os.environ,
        NOTE_AGENT_DATABASE_URL=make_conninfo(database_url, application_name=application_name),
        NOTE_AGENT_PG_WORKER_STOP_GRACE_SECONDS="0.1",
    )
    if not active:
        env["NOTE_AGENT_PG_WORKER_HANDLERS"] = ""
    if active:
        env["NOTE_AGENT_PG_WORKER_HANDLERS"] = "test_jobs_worker:cli_handlers"
        env["PYTHONPATH"] = os.path.abspath("tests/postgres")
    retry = subprocess.run(
        [sys.executable, "-m", "app.postgres_worker", "--retry", str(job_id)],
        env=env,
        capture_output=True,
        timeout=5,
    )
    assert retry.returncode == 0, retry.stderr

    def worker_connections():
        with repository.database.connection() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name=%s",
                (application_name,),
            ).fetchone()["n"]

    # Explicit empty factory must fail absent handlers, then wait idle until SIGTERM.
    process = subprocess.Popen([sys.executable, "-m", "app.postgres_worker"], env=env)
    try:
        if active:
            wait_for(lambda: read_summary(repository, note) == "running")
        else:
            wait_for(lambda: state(repository, job_id)["status"] == "failed")
            assert state(repository, job_id)["error"]["code"] == "handler_missing"
        wait_for(lambda: worker_connections() > 0, timeout=5)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 0
        if active:
            assert state(repository, job_id)["error"]["code"] == "worker_stopped"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    wait_for(lambda: worker_connections() == 0, timeout=5)


@pytest.mark.parametrize(
    "kwargs", [{"lease_seconds": 0}, {"timeout_seconds": float("nan")}, {"heartbeat_seconds": 30}]
)
def test_settings_reject_invalid_intervals(kwargs):
    with pytest.raises(ValueError):
        WorkerSettings(**kwargs)
