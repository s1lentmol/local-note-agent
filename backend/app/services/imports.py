"""Batch admission and progress using the shared T11 file import and extract job."""

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO

from psycopg import OperationalError

from app.dto import BatchImportFields, FileImportFields, ImportAccepted, ImportDetail, ImportItem
from app.errors import ProcessingError
from app.repositories.collections import CollectionsRepository
from app.repositories.imports import ImportsRepository
from app.repositories.sources import TelegramDelivery
from app.services.collections import missing_collection
from app.services.notes import NoteError

MAX_FILES = 20
MAX_TELEGRAM_FILES = 10
MAX_BATCH_BYTES = 10 * 1024 * 1024
FILE_FIELDS = FileImportFields(source_type="text_file")


@dataclass
class ImportFile:
    name: str | None
    size: int
    stream: BinaryIO | None = None
    delivery: TelegramDelivery | None = None
    error: ProcessingError | None = None

    @property
    def delivery_id(self):
        return str(self.delivery.update_id) if self.delivery else None


def item_result(row):
    note_id = row["note_id"]
    if row["status"] == "failed":
        status, error = "failed", row["error"]
    elif row["deleted_at"] is not None or note_id is None:
        note_id = None
        status = "done" if row["extract_status"] == "done" else "failed"
        error = None
        if status == "failed":
            error = row["extract_error"] or {
                "code": "note_deleted",
                "message": "Заметка удалена до завершения извлечения.",
            }
    else:
        ingestion = row["ingestion_status"]
        status = "done" if ingestion == "ready" else ingestion
        error = row["ingestion_error"] if status == "failed" else None
    return ImportItem(
        id=row["id"],
        original_name=row["original_name"],
        status=status,
        error=error,
        note_id=note_id,
    )


class ImportsService:
    def __init__(self, files):
        self.files = files
        self.database = files.database

    def accept(self, inputs: list[ImportFile], fields: BatchImportFields) -> ImportAccepted:
        if fields.collection_title and "\x00" in fields.collection_title:
            raise NoteError("invalid_collection_title", "Некорректное название подборки.")
        batch_id, collection_id, item_ids = self._accept(inputs, fields)
        return ImportAccepted(batch_id=batch_id, collection_id=collection_id, item_ids=item_ids)

    def telegram_receipt(self, scope, external_id):
        with self.database.transaction() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            repo = ImportsRepository(conn)
            batch = repo.telegram_batch(scope, external_id)
            return (batch["id"], repo.deliveries(batch["id"])) if batch else (None, set())

    def accept_telegram(self, inputs: list[ImportFile], scope: str, external_id: str):
        batch_id, _, _ = self._accept(inputs, BatchImportFields(), scope, external_id)
        return batch_id

    def _accept(self, inputs, fields, scope=None, external_id=None):
        file_limit = MAX_FILES if scope is None else MAX_TELEGRAM_FILES
        if not 1 <= len(inputs) <= file_limit:
            raise ProcessingError("invalid_file_count", f"Загрузите от 1 до {file_limit} файлов.")
        if sum(entry.size for entry in inputs) > MAX_BATCH_BYTES:
            raise ProcessingError("batch_too_large", "Общий размер файлов превышает 10 МиБ.")
        with ExitStack() as stack:
            staged = []
            for entry in inputs:
                original, error = self._stage(stack, entry)
                staged.append((entry, original, error))
            committed = []
            with self.database.transaction() as conn:
                repo = ImportsRepository(conn)
                collections = CollectionsRepository(conn)
                batch = None
                if scope is not None:
                    repo.lock_delivery(scope, external_id)
                    batch = repo.telegram_batch(scope, external_id)
                position = batch["total"] if batch else 0
                if batch:
                    seen = repo.deliveries(batch["id"])
                    staged = [item for item in staged if item[0].delivery_id not in seen]
                    if position + len(staged) > MAX_TELEGRAM_FILES:
                        raise ProcessingError(
                            "invalid_file_count", "В группе Telegram более 10 документов."
                        )
                    batch_id, collection_id = batch["id"], batch["collection_id"]
                    # Serialize against deletion. A removed collection stays removed.
                    if collection_id is not None and collections.lock(collection_id) is None:
                        collection_id = None
                    if staged:
                        repo.extend(batch_id, len(staged))
                else:
                    collection_id = self._collection(collections, fields)
                    batch_id = repo.create(collection_id, len(staged), scope, external_id)
                item_ids = []
                for entry, original, error in staged:
                    note_id = None
                    if original is not None:
                        note_id, error = self._save(
                            conn, collections, collection_id, original, entry
                        )
                        if note_id is not None:
                            committed.append(original)
                    name = (entry.name or "").replace("\x00", "") or "Файл"
                    item_ids.append(
                        repo.create_item(
                            batch_id, position, name, entry.delivery_id, note_id, error
                        )
                    )
                    position += 1
            for original in committed:
                original.committed = True
            return batch_id, collection_id, item_ids

    def _stage(self, stack, entry):
        if entry.error is not None:
            return None, entry.error.as_dict()
        try:
            original = stack.enter_context(self.files.stage(entry.stream, entry.name, FILE_FIELDS))
            return original, None
        except ProcessingError as exc:
            return None, exc.as_dict()
        except OSError:
            return None, {"code": "storage_error", "message": "Не удалось сохранить оригинал."}

    def _save(self, conn, collections, collection_id, original, entry):
        try:
            with conn.transaction():
                note_id = self.files.create_note(conn, original, delivery=entry.delivery)
                if collection_id is not None:
                    collections.add(collection_id, [note_id])
        except OperationalError:
            raise
        except Exception:
            return None, {"code": "file_import_failed", "message": "Не удалось создать заметку."}
        return note_id, None

    @staticmethod
    def _collection(collections, fields):
        if fields.collection_id is not None:
            if collections.lock(fields.collection_id) is None:
                raise missing_collection()
            return fields.collection_id
        title = fields.collection_title or f"Импорт {datetime.now(UTC):%d.%m.%Y}"
        return collections.create(title)["id"]

    def get(self, batch_id):
        with self.database.transaction() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            repo = ImportsRepository(conn)
            batch = repo.get(batch_id)
            if batch is None:
                raise NoteError("import_not_found", "Импорт не найден.")
            items = [item_result(row) for row in repo.items(batch_id)]
        succeeded = sum(item.status == "done" for item in items)
        failed = sum(item.status == "failed" for item in items)
        total = batch["total"]
        if all(item.status == "queued" for item in items):
            status = "queued"
        elif succeeded + failed < total:
            status = "processing"
        else:
            status = "done" if succeeded else "failed"
        return ImportDetail(**batch, status=status, succeeded=succeeded, failed=failed, items=items)
