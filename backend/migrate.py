"""Numbered SQL migrations. Run explicitly: python migrate.py."""

import hashlib
import re
from pathlib import Path

import psycopg

from app.config import Settings

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
LOCK_ID = 7266347200201


class MigrationError(RuntimeError):
    pass


def discover(directory: Path) -> list[tuple[int, Path, str]]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = re.fullmatch(r"([0-9]{3,})_[a-z0-9_]+\.sql", path.name)
        if not match:
            raise MigrationError(f"Invalid migration filename: {path.name}")
        number = int(match[1])
        migrations.append((number, path, hashlib.sha256(path.read_bytes()).hexdigest()))
    migrations.sort(key=lambda item: item[0])
    if not migrations or [item[0] for item in migrations] != list(range(1, len(migrations) + 1)):
        raise MigrationError("Migration numbers must be contiguous starting at 001")
    return migrations


def migrate(url: str, directory: Path = MIGRATIONS) -> list[int]:
    if not url:
        raise MigrationError("NOTE_AGENT_DATABASE_URL is required")
    migrations = discover(directory)
    applied_now = []
    # Session lock spans every per-migration transaction and is released on disconnect too.
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            with conn.transaction():
                conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                    number integer PRIMARY KEY CHECK (number > 0),
                    checksum text NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
                    applied_at timestamptz NOT NULL DEFAULT now())""")
            applied = dict(
                conn.execute("SELECT number, checksum FROM schema_migrations").fetchall()
            )
            available = {number: checksum for number, _, checksum in migrations}
            if sorted(applied) != list(range(1, len(applied) + 1)):
                raise MigrationError("Applied migration history is not contiguous")
            for number, checksum in applied.items():
                if available.get(number) != checksum:
                    raise MigrationError(
                        f"Migration {number:03}: checksum mismatch or file missing"
                    )
            for number, path, checksum in migrations:
                if number in applied:
                    continue
                try:
                    with conn.transaction():
                        conn.execute(path.read_text(encoding="utf-8"))
                        conn.execute(
                            "INSERT INTO schema_migrations (number, checksum) VALUES (%s, %s)",
                            (number, checksum),
                        )
                except psycopg.Error as exc:
                    raise MigrationError(
                        f"Migration {path.name} failed: {exc.diag.message_primary}"
                    ) from exc
                applied_now.append(number)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    return applied_now


def main() -> None:
    try:
        applied = migrate(Settings.from_env().database_url)
    except (MigrationError, psycopg.Error, ValueError) as exc:
        raise SystemExit(f"Migration error: {exc}") from exc
    print(f"Applied migrations: {applied}" if applied else "Schema is up to date")


if __name__ == "__main__":
    main()
