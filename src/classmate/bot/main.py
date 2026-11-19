import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from classmate.bot.command_menu import setup_command_menu
from classmate.bot.panel_handlers import router as panel_router
from classmate.bot.handlers import router
from classmate.bot.approval_handlers import router as approval_router
from classmate.bot.group_handlers import router as group_router
from classmate.bot.private_handlers import router as private_router
from classmate.bot.wizard_handlers import router as wizard_router
from classmate.bot.management_handlers import router as management_router
from classmate.bot.middlewares import ProcessedUpdateMiddleware
from classmate.db.fsm_storage import PostgresFSMStorage
from classmate.db.session import SessionFactory
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
    dispatcher = Dispatcher(storage=PostgresFSMStorage(SessionFactory))
    dispatcher.update.outer_middleware(ProcessedUpdateMiddleware(SessionFactory))
    dispatcher.include_router(approval_router)
    dispatcher.include_router(group_router)
    dispatcher.include_router(private_router)
    dispatcher.include_router(wizard_router)
    dispatcher.include_router(router)
    dispatcher.include_router(management_router)
    dispatcher.include_router(panel_router)
    logging.getLogger(__name__).info("Starting ClassMate bot polling")
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
