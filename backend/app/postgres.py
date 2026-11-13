"""Explicit PostgreSQL lifecycle; importing this module performs no I/O.

Open/close once per process. Services own transaction boundaries and use
``with database.transaction() as conn: conn.execute(sql, parameters)``.
Never perform extraction or model calls inside that context.
"""

from contextlib import contextmanager
from typing import Iterator

from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 5, timeout: float = 10):
        if not url or not conninfo_to_dict(url):
            raise ValueError("A PostgreSQL database URL is required")
        self.pool = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
            configure=register_vector,
        )

    def open(self) -> None:
        try:
            self.pool.open(wait=True)
        except BaseException:
            self.pool.close()
            raise

    def close(self) -> None:
        self.pool.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        """Borrow an autocommit connection; use transaction() for atomic writes."""
        with self.pool.connection() as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.connection() as conn, conn.transaction():
            yield conn
