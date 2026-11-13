import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from migrate import MIGRATIONS, MigrationError, discover, migrate


def test_empty_database_and_repeat(database_url):
    assert migrate(database_url) == [1, 2]
    assert migrate(database_url) == []
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT number, checksum FROM schema_migrations").fetchall() == [
            (number, checksum) for number, _, checksum in discover(MIGRATIONS)
        ]
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            )
        }
        assert tables == {
            "schema_migrations",
            "notes",
            "sources",
            "attachments",
            "import_batches",
            "import_items",
            "collections",
            "collection_notes",
            "jobs",
            "chunks",
            "embedding_profiles",
            "enrichments",
            "note_links",
            "conversations",
            "messages",
        }


def test_checksum_and_missing_file(database_url, tmp_path):
    path = tmp_path / "001_test.sql"
    path.write_text("CREATE TABLE original (id integer)")
    migrate(database_url, tmp_path)
    path.write_text("CREATE TABLE changed (id integer)")
    with pytest.raises(MigrationError, match="001: checksum mismatch"):
        migrate(database_url, tmp_path)
    path.rename(tmp_path / "002_test.sql")
    with pytest.raises(MigrationError, match="contiguous"):
        migrate(database_url, tmp_path)


def test_failure_rolls_back_ddl_and_history(database_url, tmp_path):
    (tmp_path / "001_good.sql").write_text("CREATE TABLE good (id integer)")
    bad = tmp_path / "002_bad.sql"
    bad.write_text("CREATE TABLE partial (id integer); SELECT 1/0")
    with pytest.raises(MigrationError, match="002_bad.sql failed: division by zero"):
        migrate(database_url, tmp_path)
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT to_regclass('partial')").fetchone() == (None,)
        assert conn.execute("SELECT number FROM schema_migrations").fetchall() == [(1,)]
    bad.write_text("CREATE TABLE partial (id integer)")
    assert migrate(database_url, tmp_path) == [2]


def test_concurrent_migrators_are_serialized(database_url, tmp_path):
    (tmp_path / "001_slow.sql").write_text(
        "SELECT pg_sleep(0.5); CREATE TABLE once_only (id integer)"
    )
    start = threading.Barrier(2)

    def run():
        start.wait(timeout=5)
        return migrate(database_url, tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.submit(run), executor.submit(run)
        assert sorted([first.result(timeout=15), second.result(timeout=15)]) == [[], [1]]
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone() == (1,)


def test_migration_discovery(tmp_path):
    with pytest.raises(MigrationError, match="contiguous"):
        discover(tmp_path)
    (tmp_path / "001_a.sql").write_text("SELECT 1")
    (tmp_path / "001_b.sql").write_text("SELECT 1")
    with pytest.raises(MigrationError, match="contiguous"):
        discover(tmp_path)


def test_missing_database_url_never_uses_libpq_defaults(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("must not connect")

    monkeypatch.setattr(psycopg, "connect", reject)
    with pytest.raises(MigrationError, match="DATABASE_URL is required"):
        migrate("")
