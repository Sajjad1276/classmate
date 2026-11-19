from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)


async def setup_command_menu(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="باز کردن منوی ClassMate"),
        BotCommand(command="help", description="راهنمای استفاده"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
