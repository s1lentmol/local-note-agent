"""SQL receipts for committed dialogue mutations; caller owns transactions."""

from psycopg.types.json import Jsonb


class ChatActionsRepository:
    def __init__(self, conn):
        self.conn = conn

    def lock_conversation(self, conversation_id):
        self.conn.execute("SELECT id FROM conversations WHERE id=%s FOR UPDATE", (conversation_id,))

    def delivered(self, conversation_id, delivery_id):
        if delivery_id is None:
            return None
        return self.conn.execute(
            "SELECT * FROM chat_actions WHERE conversation_id=%s AND delivery_id=%s",
            (conversation_id, delivery_id),
        ).fetchone()

    def action_count(self, conversation_id):
        # Each committed mutation appends one operation, including multiple
        # actions in one receipt. No long-lived claim or lock is needed.
        return self.conn.execute(
            """SELECT COALESCE(sum(jsonb_array_length(operations)), 0) AS total
               FROM chat_actions WHERE conversation_id=%s""",
            (conversation_id,),
        ).fetchone()["total"]

    def record(self, turn_id, conversation_id, delivery_id, operations):
        self.conn.execute(
            """INSERT INTO chat_actions(id,conversation_id,delivery_id,operations)
               VALUES (%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET operations=EXCLUDED.operations""",
            (turn_id, conversation_id, delivery_id, Jsonb(operations)),
        )

    def finish(self, turn_id, response):
        self.conn.execute(
            "UPDATE chat_actions SET response=%s WHERE id=%s",
            (Jsonb(response.model_dump(mode="json")), turn_id),
        )
