"""Bounded Telegram document collection within the polling process."""

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass, field
from io import BytesIO

from aiogram import Bot
from aiogram.types import Message, Update

from app.errors import ProcessingError
from app.repositories.sources import TelegramDelivery
from app.services.file_import import text_filename
from app.services.imports import MAX_TELEGRAM_FILES, ImportFile

GROUP_WINDOW_SECONDS = 1.0
MAX_PENDING_GROUPS = 16
DOWNLOAD_TIMEOUT_SECONDS = 15


class DocumentBuffer(BytesIO):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise ProcessingError("file_too_large", "Файл превышает допустимый размер.")
        return super().write(data)


@dataclass
class MediaGroup:
    documents: dict[int, Message] = field(default_factory=dict)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


class TelegramDocuments:
    def __init__(self, imports, run_sync, *, window=GROUP_WINDOW_SECONDS):
        self.imports = imports
        self.run_sync = run_sync
        self.window = window
        self.groups = {}
        self.tasks = set()
        self.slots = asyncio.Semaphore(MAX_PENDING_GROUPS)
        self.closing = False

    async def document(self, message: Message, event_update: Update, bot: Bot):
        if self.closing:
            return
        scope = f"{bot.id}:{message.chat.id}"
        delivery_id = event_update.update_id
        group_id = message.media_group_id
        if group_id is None:
            await self._import(bot, scope, f"update:{delivery_id}", [(delivery_id, message)])
            return
        key = (scope, f"group:{group_id}")
        group = self.groups.get(key)
        if group is None:
            await self.slots.acquire()
            if self.closing:
                self.slots.release()
                return
            # A dispatcher can also receive concurrently dispatched updates.
            group = self.groups.get(key)
            if group is None:
                group = MediaGroup()
                self.groups[key] = group
                group.task = asyncio.create_task(self._flush(bot, key, group))
                self.tasks.add(group.task)
                group.task.add_done_callback(self.tasks.discard)
            else:
                self.slots.release()
        group.documents.setdefault(delivery_id, message)
        if len(group.documents) >= MAX_TELEGRAM_FILES:
            group.ready.set()
        # Keep collection bounded even for updates outside Telegram's group contract.
        if group.ready.is_set():
            await asyncio.shield(group.task)

    async def _flush(self, bot, key, group):
        try:
            try:
                await asyncio.wait_for(group.ready.wait(), self.window)
            except TimeoutError:
                pass
            self.groups.pop(key)
            await self._import(bot, *key, sorted(group.documents.items()))
        finally:
            self.slots.release()

    async def close(self, **_):
        self.closing = True
        for group in self.groups.values():
            group.ready.set()
        if self.tasks:
            await asyncio.gather(*self.tasks)

    async def _import(self, bot, scope, external_id, documents):
        message = documents[0][1]
        try:
            batch_id, existing = await self.run_sync(
                self.imports.telegram_receipt, scope, external_id
            )
            with ExitStack() as stack:
                inputs = [
                    await self._download(stack, bot, update_id, document)
                    for update_id, document in documents
                    if str(update_id) not in existing
                ]
                if inputs:
                    batch_id = await self.run_sync(
                        self.imports.accept_telegram, inputs, scope, external_id
                    )
            reply = f"Импорт файлов принят: {batch_id}. Результаты доступны в разделе «Загрузка»."
        except Exception:
            reply = "Не удалось принять файлы. Отправьте их повторно."
        try:
            await asyncio.wait_for(message.answer(reply), DOWNLOAD_TIMEOUT_SECONDS)
        except Exception:
            # A failed reply must not undo or duplicate the committed batch.
            pass

    async def _download(self, stack, bot, update_id, message):
        document = message.document
        delivery = TelegramDelivery(
            bot_id=bot.id,
            update_id=update_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            metadata={"media_group_id": message.media_group_id} if message.media_group_id else {},
        )
        name = document.file_name
        result = ImportFile(name, 0, delivery=delivery)
        limit = self.imports.files.settings.text_file_max_bytes
        try:
            text_filename(name)
            if document.file_size is not None and document.file_size > limit:
                raise ProcessingError("file_too_large", "Файл превышает допустимый размер.")
            stream = stack.enter_context(DocumentBuffer(limit))
            await asyncio.wait_for(
                bot.download(document, destination=stream, timeout=DOWNLOAD_TIMEOUT_SECONDS),
                DOWNLOAD_TIMEOUT_SECONDS,
            )
            result.stream = stream
            result.size = stream.getbuffer().nbytes
        except ProcessingError as exc:
            result.error = exc
        except Exception:
            result.error = ProcessingError("telegram_download_failed", "Не удалось скачать файл.")
        return result
