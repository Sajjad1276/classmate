import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from classmate.core.config import get_settings
from classmate.core.logging import configure_logging
from classmate.db.session import dispose_engine
from classmate.presentation.telegram.router import router as telegram_router
from classmate.presentation.telegram.storage import DatabaseFSMStorage
from classmate.worker.wait_for_schema import wait_for_schema
from classmate.bot.command_menu import setup_command_menu


async def main() -> None:
    configure_logging()
    settings = get_settings()
    token = settings.bot_token.strip()
    if not token or token.startswith("SET_ME__"):
        raise RuntimeError("BOT_TOKEN is not configured.")

    await wait_for_schema()
    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = DatabaseFSMStorage()
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(telegram_router)

    try:
        await setup_command_menu(bot)
        logging.getLogger(__name__).info("Starting ClassMate V2 polling.")
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            handle_as_tasks=True,
        )
    finally:
        await storage.close()
        await bot.session.close()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
