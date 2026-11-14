"""Synthetic updates and a recording session; no Telegram network calls."""

from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Message, Update


class RecordingSession(BaseSession):
    def __init__(self, on_send=None):
        super().__init__()
        self.sent = []
        self.closed = False
        self.on_send = on_send

    async def close(self):
        self.closed = True

    async def make_request(self, bot, method, timeout=None):
        assert isinstance(method, SendMessage)
        if self.on_send:
            self.on_send(method)
        self.sent.append(method)
        return Message(message_id=999, date=1, chat={"id": 42, "type": "private"})

    async def stream_content(self, url, **kwargs):
        raise AssertionError("No files in T05")
        yield b""


def update(update_id=1, *, origin=None, **message):
    payload = dict(
        message_id=update_id,
        date=1700000000,
        chat={"id": 42, "type": "private"},
        text="  Заголовок\nТекст  ",
    )
    payload.update(message)
    if origin:
        payload["forward_origin"] = origin
    return Update(update_id=update_id, message=payload)


def forwarded(update_id=1):
    return update(
        update_id,
        origin={"type": "hidden_user", "date": 1700000000, "sender_user_name": "Synthetic"},
    )
