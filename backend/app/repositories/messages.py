"""Ordered, atomic dialogue turns in the existing messages table."""

from datetime import timedelta
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.model_clients.contracts import Message


class MessagesRepository:
    def __init__(self, conn: Connection, conversation_id: UUID):
        self.conn = conn
        self.conversation_id = conversation_id

    def recent(self, limit: int):
        return self.conn.execute(
            """SELECT id, created_at, role, text, tool_calls, tool_call_id
               FROM messages WHERE conversation_id = %s
               ORDER BY created_at DESC, id DESC LIMIT %s""",
            (self.conversation_id, limit),
        ).fetchall()

    def delivered_reply(self, delivery_id: str):
        return self.conn.execute(
            """SELECT reply.text
               FROM messages AS input
               JOIN LATERAL (
                   SELECT text FROM messages
                   WHERE conversation_id = input.conversation_id AND role = 'assistant'
                     AND tool_calls = '[]'::jsonb
                     AND (created_at, id) > (input.created_at, input.id)
                   ORDER BY created_at, id LIMIT 1
               ) AS reply ON true
               WHERE input.conversation_id = %s AND input.delivery_id = %s
                 AND input.role = 'user'""",
            (self.conversation_id, delivery_id),
        ).fetchone()

    def append_turn(self, messages: list[Message], delivery_id: str | None, previous):
        # Caller holds the conversation row lock. Distinct timestamps preserve the
        # protocol order even though all rows are written in one transaction.
        start = self.conn.execute("SELECT clock_timestamp() AS ts").fetchone()["ts"]
        if previous is not None:
            start = max(start, previous["created_at"] + timedelta(microseconds=1))
        for index, message in enumerate(messages):
            self.conn.execute(
                """INSERT INTO messages
                       (conversation_id, role, text, tool_calls, tool_call_id, delivery_id,
                        created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    self.conversation_id,
                    message.role,
                    message.content or "",
                    Jsonb([call.model_dump(mode="json") for call in message.tool_calls]),
                    message.tool_call_id,
                    delivery_id if index == 0 else None,
                    start + timedelta(microseconds=index),
                ),
            )
