"""Original materials are immutable during note edits."""

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field


class TelegramDelivery(BaseModel):
    """Internal ingestion context; never contains a bot token."""

    bot_id: int = Field(gt=0)
    update_id: int = Field(ge=0)
    chat_id: int
    message_id: int = Field(gt=0)
    metadata: dict = Field(default_factory=dict)


class DuplicateDelivery(Exception):
    pass


class SourcesRepository:
    def __init__(self, conn):
        self.conn = conn

    def create_file(self, note_id):
        return self.conn.execute(
            "INSERT INTO sources(note_id,type) VALUES (%s,'text_file') RETURNING id",
            (note_id,),
        ).fetchone()["id"]

    def create_text(self, note_id, text):
        self.conn.execute(
            "INSERT INTO sources (note_id, type, original_text) VALUES (%s, 'text', %s)",
            (note_id, text),
        )

    def create_telegram_text(self, note_id, text, delivery: TelegramDelivery):
        self._create_telegram(note_id, "text", text, delivery)

    def create_telegram_file(self, note_id, delivery: TelegramDelivery):
        return self._create_telegram(note_id, "text_file", None, delivery)

    def _create_telegram(self, note_id, source_type, text, delivery):
        row = self.conn.execute(
            """INSERT INTO sources (note_id, type, original_text, channel, delivery_scope,
                   delivery_id, telegram_chat_id, telegram_message_id, metadata)
               VALUES (%s, %s, %s, 'telegram', %s, %s, %s, %s, %s)
               ON CONFLICT (channel, delivery_scope, delivery_id) DO NOTHING RETURNING id""",
            (
                note_id,
                source_type,
                text,
                str(delivery.bot_id),
                str(delivery.update_id),
                delivery.chat_id,
                delivery.message_id,
                Jsonb(delivery.metadata),
            ),
        ).fetchone()
        if row is None:
            raise DuplicateDelivery
        return row["id"]

    def telegram_note_id(self, delivery: TelegramDelivery):
        return self.conn.execute(
            """SELECT note_id FROM sources WHERE channel='telegram'
               AND delivery_scope=%s AND delivery_id=%s""",
            (str(delivery.bot_id), str(delivery.update_id)),
        ).fetchone()["note_id"]

    def attachments(self, note_id):
        return self.conn.execute(
            """SELECT a.id, a.original_name, a.mime_type, a.size_bytes,
                      '/api/v1/attachments/' || a.id || '/download' AS download_url
               FROM attachments a JOIN sources s ON s.id = a.source_id
               WHERE s.note_id = %s ORDER BY a.created_at, a.id""",
            (note_id,),
        ).fetchall()
