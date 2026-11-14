"""Short, atomic service calls plus durable action receipts. No model I/O."""

from contextlib import contextmanager

from app.dto import ChatResponse
from app.repositories.chat_actions import ChatActionsRepository


class TransactionDatabase:
    """Let existing application services join the action's SQL transaction."""

    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def transaction(self):
        with self.conn.transaction():
            yield self.conn

    @contextmanager
    def connection(self):
        yield self.conn


def action_report(conversation_id, operations):
    labels = {
        "save_note": "Заметка сохранена",
        "update_note": "Заметка изменена",
        "create_collection": "Подборка создана",
        "add_to_collection": "Заметка добавлена в подборку",
        "remove_from_collection": "Заметка исключена из подборки",
    }
    return ChatResponse(
        conversation_id=conversation_id,
        text="\n".join(f"{labels[op['type']]}: {op['id']}." for op in operations),
        operations=operations,
    )


def receipt(conn, conversation_id, delivery_id):
    row = ChatActionsRepository(conn).delivered(conversation_id, delivery_id)
    if row is None:
        return None
    return (
        ChatResponse.model_validate(row["response"])
        if row["response"]
        else action_report(conversation_id, row["operations"])
    )


def record_response(conn, turn_id, response):
    ChatActionsRepository(conn).finish(turn_id, response)
