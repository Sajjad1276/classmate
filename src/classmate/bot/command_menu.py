from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

PRIVATE_COMMANDS = [
    BotCommand(command="start", description="شروع و نمایش منوی اصلی"),
    BotCommand(command="help", description="راهنمای کامل فرمان‌ها"),
    BotCommand(command="classes", description="نمایش کلاس‌های من"),
    BotCommand(command="join", description="عضویت با کد دعوت کلاس"),
    BotCommand(command="new_homework", description="ثبت تکلیف جدید"),
    BotCommand(command="new_exam", description="ثبت امتحان جدید"),
    BotCommand(command="new_report", description="ثبت گزارش روزانه"),
    BotCommand(command="homework", description="مشاهده تکالیف تأییدشده"),
    BotCommand(command="exams", description="مشاهده امتحانات تأییدشده"),
    BotCommand(command="today", description="گزارش تأییدشده امروز"),
    BotCommand(command="week", description="نمایش برنامه هفتگی"),
    BotCommand(command="ask", description="جست‌وجو در اطلاعات کلاس"),
    BotCommand(command="reminders_on", description="فعال‌سازی یادآوری‌ها"),
    BotCommand(command="reminders_off", description="غیرفعال‌سازی یادآوری‌ها"),
    BotCommand(command="cancel", description="لغو فرایند ثبت جاری"),
]

GROUP_COMMANDS = [
    BotCommand(command="help", description="راهنمای فرمان‌های گروه"),
    BotCommand(command="new_homework", description="ثبت تکلیف جدید"),
    BotCommand(command="new_exam", description="ثبت امتحان جدید"),
    BotCommand(command="new_report", description="ثبت گزارش روزانه"),
    BotCommand(command="homework", description="مشاهده تکالیف تأییدشده"),
    BotCommand(command="exams", description="مشاهده امتحانات تأییدشده"),
    BotCommand(command="today", description="گزارش تأییدشده امروز"),
    BotCommand(command="week", description="نمایش برنامه هفتگی"),
    BotCommand(command="ask", description="جست‌وجو در اطلاعات کلاس"),
    BotCommand(command="pending", description="صف موارد نیازمند تأیید"),
    BotCommand(command="cancel", description="لغو فرایند ثبت جاری"),
]

GROUP_ADMIN_COMMANDS = GROUP_COMMANDS + [
    BotCommand(command="connect", description="اتصال این گروه به یک کلاس"),
    BotCommand(command="subject_add", description="افزودن درس به کلاس"),
    BotCommand(command="timetable_set", description="ثبت یا تغییر یک زنگ برنامه"),
]


def _append_unique(commands: list[BotCommand], additions: list[BotCommand]) -> list[BotCommand]:
    seen = {item.command for item in commands}
    return commands + [item for item in additions if item.command not in seen]


async def setup_command_menu(bot: Bot) -> None:
    """Install context-aware Telegram command menus; handlers still enforce permissions."""
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="شروع و نمایش منوی اصلی"),
            BotCommand(command="help", description="راهنمای فرمان‌ها"),
        ],
        scope=BotCommandScopeDefault(),
    )
    await bot.set_my_commands(
        PRIVATE_COMMANDS,
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        GROUP_COMMANDS,
        scope=BotCommandScopeAllGroupChats(),
    )
    await bot.set_my_commands(
        GROUP_ADMIN_COMMANDS,
        scope=BotCommandScopeAllChatAdministrators(),
    )


async def sync_private_command_menu(
    bot: Bot,
    chat_id: int,
    *,
    is_platform_admin: bool,
    is_school_admin: bool,
    is_class_manager: bool,
    is_approver: bool,
) -> None:
    """Show management commands only to users with matching application roles."""
    commands = list(PRIVATE_COMMANDS)

    if is_platform_admin:
        commands = _append_unique(
            commands,
            [
                BotCommand(command="school_new", description="ایجاد مدرسه جدید"),
                BotCommand(command="school_admin_add", description="تعیین مدیر مدرسه"),
            ],
        )

    if is_school_admin:
        commands = _append_unique(
            commands,
            [
                BotCommand(command="year_new", description="ایجاد و فعال‌سازی سال تحصیلی"),
                BotCommand(command="class_new", description="ایجاد کلاس در مدرسه"),
                BotCommand(command="class_manager_add", description="تعیین مدیر کلاس"),
                BotCommand(command="class_code", description="ساخت کد اتصال و عضویت کلاس"),
                BotCommand(command="approver_add", description="تعیین فرد مورد اعتماد"),
                BotCommand(command="subject_add", description="افزودن درس به کلاس"),
                BotCommand(command="timetable_set", description="ثبت یا تغییر برنامه هفتگی"),
            ],
        )

    if is_class_manager:
        commands = _append_unique(
            commands,
            [
                BotCommand(command="class_code", description="ساخت کد اتصال و عضویت کلاس"),
                BotCommand(command="approver_add", description="تعیین فرد مورد اعتماد"),
                BotCommand(command="subject_add", description="افزودن درس به کلاس"),
                BotCommand(command="timetable_set", description="ثبت یا تغییر برنامه هفتگی"),
                BotCommand(command="pending", description="صف موارد نیازمند تأیید"),
            ],
        )

    if is_approver:
        commands = _append_unique(
            commands,
            [BotCommand(command="pending", description="صف موارد نیازمند تأیید")],
        )

    await bot.set_my_commands(
        commands,
        scope=BotCommandScopeChat(chat_id=chat_id),
    )
