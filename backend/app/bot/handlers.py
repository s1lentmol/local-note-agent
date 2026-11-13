import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, Update

from app.dto import TextNoteCreate
from app.repositories.sources import TelegramDelivery
from app.services.notes import NotesService


async def save_text(
    message: Message,
    event_update: Update,
    bot: Bot,
    notes: NotesService,
):
    try:
        text = message.text
        metadata = {}
        if message.forward_origin is not None:
            metadata["forward_origin"] = message.forward_origin.model_dump(
                mode="json", exclude_none=True
            )
        if message.is_automatic_forward is not None:
            metadata["is_automatic_forward"] = message.is_automatic_forward
        request = TextNoteCreate(source_type="text", text=text, title=None)
        delivery = TelegramDelivery(
            bot_id=bot.id,
            update_id=event_update.update_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            metadata=metadata,
        )
        pending = asyncio.create_task(asyncio.to_thread(notes.create, request, delivery=delivery))
        try:
            note = await asyncio.shield(pending)
        except asyncio.CancelledError:
            # A cancelled polling task must not leave a DB thread using a closed pool.
            try:
                await pending
            except Exception:
                pass
            raise
    except Exception:
        # Never log database exceptions, message content or provider credentials.
        await message.answer("Не удалось сохранить заметку. Отправьте сообщение повторно.")
        return
    # The synchronous application call has exited its transaction (commit included).
    # A Telegram reply failure does not undo the committed note; delivery is idempotent.
    await message.answer(f"Заметка сохранена: {note.id}")


def create_dispatcher(notes: NotesService) -> Dispatcher:
    dispatcher = Dispatcher(notes=notes)
    dispatcher.message.register(save_text, F.text)
    return dispatcher
