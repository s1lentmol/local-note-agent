"""Persistence for channel-scoped conversations."""

from uuid import UUID

from psycopg import Connection


class ConversationsRepository:
    def __init__(self, conn: Connection):
        self.conn = conn

    def create_web(self, collection_id: UUID | None):
        return self.conn.execute(
            """INSERT INTO conversations (channel, collection_id) VALUES ('web', %s)
               RETURNING id, collection_id""",
            (collection_id,),
        ).fetchone()

    def telegram(self, bot_id: int, chat_id: int):
        keys = (str(bot_id), str(chat_id))
        self.conn.execute(
            """INSERT INTO conversations (channel, delivery_scope, external_id)
               VALUES ('telegram', %s, %s)
               ON CONFLICT (channel, delivery_scope, external_id) DO NOTHING""",
            keys,
        )
        return self.conn.execute(
            """SELECT id, collection_id FROM conversations
               WHERE channel = 'telegram' AND delivery_scope = %s AND external_id = %s""",
            keys,
        ).fetchone()

    def lock(self, conversation_id: UUID, channel: str):
        return self.conn.execute(
            """SELECT id, collection_id FROM conversations
               WHERE id = %s AND channel = %s FOR UPDATE""",
            (conversation_id, channel),
        ).fetchone()

    def collection_exists(self, collection_id: UUID) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM collections WHERE id = %s", (collection_id,)
            ).fetchone()
            is not None
        )

    def contains_note(self, collection_id: UUID, note_id: UUID) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM collection_notes WHERE collection_id = %s AND note_id = %s",
                (collection_id, note_id),
            ).fetchone()
            is not None
        )
