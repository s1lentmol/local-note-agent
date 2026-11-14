from app.dto import CollectionDetail, CollectionSummary, NoteListQuery
from app.repositories.collections import CollectionsRepository
from app.repositories.notes import NotesRepository
from app.services.notes import NoteError


def missing_collection():
    return NoteError("collection_not_found", "Подборка не найдена.")


class CollectionsService:
    def __init__(self, database):
        self.database = database

    def create(self, title):
        if "\x00" in title:
            raise NoteError("invalid_collection_title", "Некорректное название подборки.")
        with self.database.transaction() as conn:
            return CollectionSummary.model_validate(CollectionsRepository(conn).create(title))

    def list(self):
        with self.database.connection() as conn:
            return [
                CollectionSummary.model_validate(row) for row in CollectionsRepository(conn).list()
            ]

    def get(self, collection_id, limit, offset):
        with self.database.transaction() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            row = CollectionsRepository(conn).get(collection_id)
            if row is None:
                raise missing_collection()
            row["notes"] = NotesRepository(conn).list(
                NoteListQuery(limit=limit, offset=offset), collection_id=collection_id
            )
            return CollectionDetail.model_validate(row)

    def add(self, collection_id, note_ids):
        unique_ids = list(set(note_ids))
        with self.database.transaction() as conn:
            repo = CollectionsRepository(conn)
            if repo.lock(collection_id) is None:
                raise missing_collection()
            if len(repo.lock_live_notes(unique_ids)) != len(unique_ids):
                raise NoteError("note_not_found", "Заметка не найдена.")
            repo.add(collection_id, unique_ids)

    def remove(self, collection_id, note_id):
        with self.database.transaction() as conn:
            repo = CollectionsRepository(conn)
            if repo.lock(collection_id) is None:
                raise missing_collection()
            repo.remove(collection_id, note_id)

    def delete(self, collection_id):
        with self.database.transaction() as conn:
            if not CollectionsRepository(conn).delete(collection_id):
                raise missing_collection()
