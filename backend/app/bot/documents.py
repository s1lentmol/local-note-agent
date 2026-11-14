"""Bounded Telegram file collection within the polling process."""

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass, field
from tempfile import SpooledTemporaryFile

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, Update

from app.errors import ProcessingError
from app.repositories.sources import TelegramDelivery
from app.services.file_import import infer_source_type, text_filename
from app.services.imports import MAX_TELEGRAM_FILES, ImportFile

GROUP_WINDOW_SECONDS = 1.0
MAX_PENDING_GROUPS = 16
DOWNLOAD_TIMEOUT_SECONDS = 15
# The hosted Telegram Bot API limits getFile independently of our upload settings.
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024


def telegram_size_error():
    return ProcessingError(
        "telegram_file_too_large", "Telegram Bot API не позволяет скачать файл больше 20 МБ."
    )


class DownloadBuffer(SpooledTemporaryFile):
    def __init__(self, application_limit):
        super().__init__(max_size=1024 * 1024, mode="w+b")
        self.application_limit = application_limit

    def check_size(self, size):
        if size > self.application_limit:
            raise ProcessingError("file_too_large", "Файл превышает допустимый размер.")
        if size > TELEGRAM_MAX_BYTES:
            raise telegram_size_error()

    def write(self, data):
        self.check_size(self.tell() + len(data))
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
            if external_id.startswith("update:"):
                batch = await self.run_sync(self.imports.get, batch_id)
                item = batch.items[0]
                if item.note_id is None and item.error is not None:
                    reply = f"{item.error.message} Пачка: {batch_id}."
        except ProcessingError as exc:
            reply = exc.message
        except Exception:
            reply = "Не удалось принять файлы. Отправьте их повторно."
        try:
            await asyncio.wait_for(message.answer(reply), DOWNLOAD_TIMEOUT_SECONDS)
        except Exception:
            # A failed reply must not undo or duplicate the committed batch.
            pass

    async def _download(self, stack, bot, update_id, message):
        media = message.voice or message.audio or message.document
        name = getattr(media, "file_name", None)
        mime = media.mime_type
        metadata = {}
        if message.media_group_id:
            metadata["media_group_id"] = message.media_group_id
        if message.forward_origin is not None:
            metadata["forward_origin"] = message.forward_origin.model_dump(
                mode="json", exclude_none=True
            )
        if message.is_automatic_forward is not None:
            metadata["is_automatic_forward"] = message.is_automatic_forward
        source_type = infer_source_type(name, mime)
        if message.voice is not None or message.audio is not None:
            source_type = "audio"
            name = name or ("voice.ogg" if message.voice is not None else "Аудиозапись")
        if source_type == "audio":
            metadata["telegram_media_type"] = (
                "voice" if message.voice else "audio" if message.audio else "document"
            )
            if mime is not None:
                metadata["telegram_mime_type"] = mime
        delivery = TelegramDelivery(
            bot_id=bot.id,
            update_id=update_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            metadata=metadata,
        )
        result = ImportFile(name, 0, delivery=delivery, source_type=source_type)
        limit = self.imports.files.max_bytes(source_type)
        try:
            if source_type == "text_file":
                text_filename(name)
            stream = stack.enter_context(DownloadBuffer(limit))
            stream.check_size(media.file_size or 0)
            async with asyncio.timeout(DOWNLOAD_TIMEOUT_SECONDS):
                remote = await bot.get_file(media.file_id)
                stream.check_size(remote.file_size or 0)
                if not remote.file_path:
                    raise ProcessingError(
                        "telegram_download_failed", "Не удалось скачать файл из Telegram."
                    )
                await bot.download_file(
                    remote.file_path,
                    destination=stream,
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                    seek=False,
                )
            result.size = stream.tell()
            if remote.file_size is not None and remote.file_size != result.size:
                raise ProcessingError("telegram_download_failed", "Telegram вернул неполный файл.")
            stream.seek(0)
            result.stream = stream
        except ProcessingError as exc:
            result.error = exc
            result.size = 0
        except TelegramBadRequest as exc:
            if "file is too big" in exc.message.lower():
                result.error = telegram_size_error()
            else:
                result.error = ProcessingError(
                    "telegram_download_failed", "Не удалось скачать файл из Telegram."
                )
        except Exception:
            result.error = ProcessingError(
                "telegram_download_failed", "Не удалось скачать файл из Telegram."
            )
        return result
