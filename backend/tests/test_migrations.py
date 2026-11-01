import sqlite3

from sqlalchemy import inspect, text

from app.db import create_sqlite_engine
from app.migrate import migrate


def test_original_database_is_upgraded_without_data_loss(tmp_path):
    path = tmp_path / "notes.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE notes (id VARCHAR(36) PRIMARY KEY, title VARCHAR(200) NOT NULL,
                extracted_text TEXT NOT NULL, content_version INTEGER NOT NULL,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);
            CREATE TABLE sources (id VARCHAR(36) PRIMARY KEY, note_id VARCHAR(36) NOT NULL UNIQUE
                REFERENCES notes(id) ON DELETE CASCADE, type VARCHAR(20) NOT NULL, original_text TEXT NOT NULL);
            INSERT INTO notes VALUES ('n1', 'Title', 'Edited', 2, '2026-10-22', '2026-10-23');
            INSERT INTO sources VALUES ('s1', 'n1', 'text', 'Original');
        """)
    engine = create_sqlite_engine(path)
    try:
        migrate(engine)
        migrate(engine)
        with engine.connect() as db:
            assert db.execute(text("SELECT original_text FROM sources")).scalar() == "Original"
            row = db.execute(
                text("SELECT extracted_text, content_version, ingestion_status FROM notes")
            ).one()
            assert tuple(row) == ("Edited", 2, "ready")
            assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1
            assert not db.execute(text("PRAGMA foreign_key_check")).all()
            assert {"jobs", "attachments", "worker_state"} <= set(inspect(db).get_table_names())
    finally:
        engine.dispose()
