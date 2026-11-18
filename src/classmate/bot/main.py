import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from classmate.bot.command_menu import setup_command_menu
from classmate.bot.handlers import router
from classmate.core.config import get_settings
from classmate.core.logging import configure_logging


async def main() -> None:
    configure_logging()
    settings = get_settings()
    token = settings.bot_token.strip()
    if not token or token.startswith("SET_ME__"):
        raise RuntimeError(
            "BOT_TOKEN is not configured. Replace the Railway placeholder with a token from @BotFather."
        )
    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await setup_command_menu(bot)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    logging.getLogger(__name__).info("Starting ClassMate bot polling")
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
