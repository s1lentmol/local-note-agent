import asyncio
import logging

from aiogram import Bot

from app.bot.handlers import create_dispatcher
from app.bot.settings import BotSettings
from app.config import Settings
from app.postgres import Database
from app.services.chat import ChatService
from app.services.notes import NotesService
from app.services.search import SearchService


async def run(settings: BotSettings, application: Settings):
    bot = Bot(settings.token.get_secret_value())
    database = None
    try:
        database = Database(application.database_url)
        database.open()
        notes = NotesService(database, processing_enabled=application.processing_enabled)
        chat = ChatService.from_env(database, notes, SearchService(database))
        dispatcher = create_dispatcher(notes, chat)
        # Sequential handling also makes shutdown wait for the current request.
        # Pending updates are kept; no webhook deletion/drop_pending_updates.
        await dispatcher.start_polling(
            bot, allowed_updates=["message"], handle_as_tasks=False, close_bot_session=False
        )
    finally:
        try:
            if database is not None:
                database.close()
        finally:
            await bot.session.close()


def main():
    # aiogram polling errors can include remote exception text. The local CLI only
    # emits a fixed error code, never traceback/URLs/token from transport failures.
    logging.getLogger("aiogram").setLevel(100)
    try:
        asyncio.run(run(BotSettings.from_env(), Settings.from_env()))
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit(
            "telegram_bot_failed: проверьте настройки и доступность сервисов"
        ) from None


if __name__ == "__main__":
    main()
