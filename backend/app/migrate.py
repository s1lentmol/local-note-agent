import fcntl
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect


def migrate(engine):
    """Upgrade fresh and original CRUD databases without dropping existing notes."""
    path = Path(engine.url.database)
    with path.with_suffix(".schema.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with engine.connect() as connection:
            # SQLite batch migration recreates parent tables; disable cascades for this connection only.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            config = Config()
            config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
            config.attributes["connection"] = connection
            try:
                with connection.begin():
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    inspector = inspect(connection)
                    tables = set(inspector.get_table_names())
                    if "notes" in tables and "alembic_version" not in tables:
                        expected = {
                            "id",
                            "title",
                            "extracted_text",
                            "content_version",
                            "created_at",
                            "updated_at",
                        }
                        source_expected = {"id", "note_id", "type", "original_text"}
                        if (
                            set(c["name"] for c in inspector.get_columns("notes")) != expected
                            or "sources" not in tables
                            or set(c["name"] for c in inspector.get_columns("sources"))
                            != source_expected
                        ):
                            raise RuntimeError(
                                "Unknown database schema; automatic migration refused."
                            )
                        command.stamp(config, "001")
                    command.upgrade(config, "head")
                    if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
                        raise RuntimeError("Foreign key check failed after migration.")
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
