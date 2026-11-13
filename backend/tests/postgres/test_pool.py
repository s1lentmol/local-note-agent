import subprocess
import sys

import psycopg
import pytest
from pgvector import Vector

from app.postgres import Database
from migrate import migrate


def test_imports_and_construction_do_not_connect():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import psycopg

def reject(*a, **kw):
    raise AssertionError('unexpected connection')
psycopg.connect = reject
from app.postgres import Database
import app.dto
import migrate
pool = Database('postgresql://unreachable.invalid/unused')
assert pool.pool.closed
pool.close()
""",
        ],
        check=True,
    )


def test_pool_transactions_and_resource_release(database_url):
    migrate(database_url)
    with Database(database_url, min_size=1, max_size=1) as database:
        with database.transaction() as conn:
            backend_pid = conn.info.backend_pid
            conn.execute("CREATE TABLE pool_test (value text)")
            conn.execute("INSERT INTO pool_test VALUES (%s)", ("'); DROP TABLE notes; --",))
        with pytest.raises(psycopg.errors.DivisionByZero):
            with database.transaction() as conn:
                conn.execute("INSERT INTO pool_test VALUES ('rolled back')")
                conn.execute("SELECT 1/0")
        with database.connection() as conn:
            assert conn.info.backend_pid == backend_pid
            vector = Vector([1.0] + [0.0] * 1023)
            stored = conn.execute("SELECT %s::vector AS embedding", (vector,)).fetchone()[
                "embedding"
            ]
            assert len(stored.to_list()) == 1024
            assert stored.to_list()[0] == 1.0
            assert len(conn.execute("SELECT * FROM pool_test").fetchall()) == 1
            assert conn.execute("SELECT vector_dims(%s::vector)", ("[1,2]",)).fetchone() == {
                "vector_dims": 2
            }
        with database.transaction() as conn:
            with pytest.raises(RuntimeError):
                with conn.transaction():
                    conn.execute("INSERT INTO pool_test VALUES ('savepoint rollback')")
                    raise RuntimeError("abort savepoint")
            assert conn.execute("SELECT count(*) AS n FROM pool_test").fetchone()["n"] == 1
        assert database.pool.get_stats()["pool_available"] == 1
    assert database.pool.closed
    with psycopg.connect(database_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE pid=%s", (backend_pid,)
        ).fetchone() == (0,)
