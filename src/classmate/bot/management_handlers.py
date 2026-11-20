from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from classmate.bot.context import _user
from classmate.db.models import Subject, TimetableEntry
from classmate.db.session import SessionFactory
from classmate.services.permissions import can_manage_class, get_class_with_school, is_school_admin
from classmate.services.records import (
    audit,
    soft_delete_class_subject,
    soft_delete_timetable_entry,
    soft_delete_subject,
    restore_subject,
)

router = Router(name="classmate_management")


@router.message(Command("subject_remove"))
async def subject_remove_command(message: Message) -> None:
    if message.chat.type != "private":
        await message.answer("برای حذف درس، این فرمان را در گفت‌وگوی خصوصی ربات اجرا کن.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not all(part.isdigit() for part in parts[1:]):
        await message.answer("قالب: /subject_remove شناسه_کلاس شناسه_درس")
        return
    class_id, subject_id = map(int, parts[1:])
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=True)
            context = await get_class_with_school(session, class_id)
            if not context or not await can_manage_class(session, user, class_id):
                await message.answer("کلاس پیدا نشد یا مجوز مدیریت آن را نداری.")
                return
            class_group, _, school = context
            subject = await session.get(Subject, subject_id)
            if subject is None:
                await message.answer("درس پیدا نشد.")
                return
            removed = await soft_delete_class_subject(session, class_id, subject_id)
            if not removed:
                await message.answer("این درس به کلاس متصل نیست یا قبلاً حذف شده است.")
                return
            await audit(
                session, school.id, class_id, user.id, "subject_remove",
                "class_subject", subject_id,
                {"subject": subject.name, "deleted": True},
                {"deleted": False},
            )
            class_name, subject_name = class_group.name, subject.name
    await message.answer(
        f"✅ درس «{subject_name}» از کلاس «{class_name}» حذف نرم شد. "
        "اطلاعات تاریخی درس در پایگاه داده باقی مانده است."
    )


@router.message(Command("timetable_remove"))
async def timetable_remove_command(message: Message) -> None:
    if message.chat.type != "private":
        await message.answer("برای حذف زنگ برنامه، این فرمان را در گفت‌وگوی خصوصی ربات اجرا کن.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not all(part.isdigit() for part in parts[1:]):
        await message.answer("قالب: /timetable_remove شناسه_کلاس شناسه_زنگ")
        return
    class_id, entry_id = map(int, parts[1:])
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=True)
            context = await get_class_with_school(session, class_id)
            if not context or not await can_manage_class(session, user, class_id):
                await message.answer("کلاس پیدا نشد یا مجوز مدیریت آن را نداری.")
                return
            _, _, school = context
            entry = await session.get(TimetableEntry, entry_id)
            if entry is None or entry.class_id != class_id or entry.deleted_at is not None:
                await message.answer("زنگ برنامه پیدا نشد یا قبلاً حذف شده است.")
                return
            before = {"weekday": entry.weekday, "period": entry.period, "subject_id": entry.subject_id}
            removed = await soft_delete_timetable_entry(session, entry_id)
            if not removed:
                await message.answer("حذف زنگ انجام نشد؛ احتمالاً قبلاً حذف شده است.")
                return
            await audit(
                session, school.id, class_id, user.id, "timetable_remove",
                "timetable_entry", entry_id,
                {**before, "deleted": True},
                {**before, "deleted": False},
            )
    await message.answer("✅ زنگ برنامه حذف نرم شد؛ تاریخچه و ارتباطات قبلی حفظ شده‌اند.")


@router.message(Command("subject_archive"))
async def subject_archive_command(message: Message) -> None:
    if message.chat.type != "private":
        await message.answer("برای آرشیو سراسری درس، فرمان را در گفت‌وگوی خصوصی ربات اجرا کن.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("قالب: /subject_archive شناسه_درس")
        return
    subject_id = int(parts[1])
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=True)
            subject = await session.get(Subject, subject_id)
            if subject is None:
                await message.answer("درس پیدا نشد.")
                return
            if not await is_school_admin(session, user, subject.school_id):
                await message.answer("فقط مدیر همان مدرسه یا مدیر کل می‌تواند درس را در سطح مدرسه آرشیو کند.")
                return
            archived = await soft_delete_subject(session, subject_id)
            if not archived:
                await message.answer("این درس قبلاً آرشیو شده است.")
                return
            await audit(
                session, subject.school_id, None, user.id, "subject_archive",
                "subject", subject_id,
                {"deleted": True, "name": subject.name},
                {"deleted": False, "name": subject.name},
            )
            subject_name = subject.name
    await message.answer(
        f"✅ درس «{subject_name}» در سطح مدرسه آرشیو شد؛ در همه کلاس‌ها و برنامه‌ها پنهان است، اما سوابق تاریخی حذف نشده‌اند."
    )


@router.message(Command("subject_restore"))
async def subject_restore_command(message: Message) -> None:
    if message.chat.type != "private":
        await message.answer("برای بازیابی درس آرشیوشده، فرمان را در گفت‌وگوی خصوصی ربات اجرا کن.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("قالب: /subject_restore شناسه_درس")
        return
    subject_id = int(parts[1])
    async with SessionFactory() as session:
        async with session.begin():
            user = await _user(session, message.from_user, private=True)
            subject = await session.get(Subject, subject_id)
            if subject is None:
                await message.answer("درس پیدا نشد.")
                return
            if not await is_school_admin(session, user, subject.school_id):
                await message.answer("فقط مدیر همان مدرسه یا مدیر کل می‌تواند درس را بازیابی کند.")
                return
            restored = await restore_subject(session, subject_id)
            if not restored:
                await message.answer("این درس آرشیو نشده است.")
                return
            await audit(
                session, subject.school_id, None, user.id, "subject_restore",
                "subject", subject_id,
                {"deleted": False, "name": subject.name},
                {"deleted": True, "name": subject.name},
            )
            subject_name = subject.name
    await message.answer(
        f"✅ درس «{subject_name}» بازیابی شد و اتصال‌های قبلی کلاس‌ها و زنگ‌های برنامه نیز برگردانده شدند."
    )
