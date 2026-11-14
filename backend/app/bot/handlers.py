import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, Update

from app.bot.documents import TelegramDocuments
from app.dto import ChatRequest, TextNoteCreate
from app.repositories.sources import TelegramDelivery
from app.services.chat import ChatError, ChatService, TelegramChatDelivery
from app.services.notes import NotesService


async def run_sync(operation, *args, **kwargs):
    pending = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        # Drain the thread before polling shutdown closes the database pool.
        try:
            await pending
        except Exception:
            pass
        raise


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
        note = await run_sync(notes.create, request, delivery=delivery)
    except Exception:
        # Never log database exceptions, message content or provider credentials.
        await message.answer("Не удалось сохранить заметку. Отправьте сообщение повторно.")
        return
    # The synchronous application call has exited its transaction (commit included).
    # A Telegram reply failure does not undo the committed note; delivery is idempotent.
    await message.answer(f"Заметка сохранена: {note.id}")


async def chat_text(message: Message, event_update: Update, bot: Bot, chat: ChatService):
    delivery = TelegramChatDelivery(
        bot.id, message.chat.id, event_update.update_id, message.message_id
    )
    try:
        response = await run_sync(chat.reply, ChatRequest(message=message.text), delivery=delivery)
    except ChatError as exc:
        await message.answer(exc.message)
        return
    except Exception:
        await message.answer("Не удалось ответить. Отправьте вопрос повторно.")
        return
    await message.answer(response.text)


def create_dispatcher(
    notes: NotesService, chat: ChatService, imports=None, *, group_window=None, voice_chat=None
) -> Dispatcher:
    dispatcher = Dispatcher(notes=notes, chat=chat)
    dispatcher.message.register(
        save_text, F.text, F.forward_origin.is_not(None) | F.is_automatic_forward.is_(True)
    )
    dispatcher.message.register(chat_text, F.text)
    if imports is not None:
        options = {"window": group_window} if group_window is not None else {}
        if voice_chat is None:
            from app.services.voice_chat import VoiceChatService

            voice_chat = VoiceChatService(chat, imports.files)
        documents = TelegramDocuments(imports, run_sync, voice_chat=voice_chat, **options)
        dispatcher.message.register(documents.document, F.document | F.voice | F.audio)
        dispatcher.shutdown.register(documents.close)
    return dispatcher
