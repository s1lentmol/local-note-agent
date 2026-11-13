import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from migrate import migrate


@pytest.fixture
def database_url():
    url = os.environ.get("NOTE_AGENT_TEST_DATABASE_URL")
    if not url or conninfo_to_dict(url).get("dbname") != "t02_tests":
        pytest.fail("Run make test-db: a dedicated database named t02_tests is required")
    schema = "t02_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(url, options=f"-csearch_path={schema},public")
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def db(database_url):
    migrate(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        yield conn
