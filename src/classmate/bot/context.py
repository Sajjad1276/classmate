from __future__ import annotations

from aiogram.types import Message
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.bot.keyboards import main_reply_keyboard
from classmate.core.config import get_settings
from classmate.db.models import (
    AcademicYear,
    ClassGroup,
    ClassMembership,
    ClassSubject,
    SchoolMembership,
    Subject,
    User,
)
from classmate.domain.enums import Role
from classmate.services.permissions import ensure_user, get_class_with_school, user_can_access_class
from classmate.services.records import get_group_context

WEEKDAYS = ("شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه")


async def _user(session: AsyncSession, tg_user, private: bool = False) -> User:
    settings = get_settings()
    user = await ensure_user(session, tg_user, private_chat_started=private)
    if tg_user.id in settings.bootstrap_admin_id_set:
        user.is_platform_admin = True
    return user


async def _main_keyboard_for_user(session: AsyncSession, user: User):
    platform_admin = user.is_platform_admin
    school_admin = platform_admin or (
        await session.scalar(
            select(SchoolMembership.id).where(
                SchoolMembership.user_id == user.id,
                SchoolMembership.role == Role.SCHOOL_ADMIN,
                SchoolMembership.is_active.is_(True),
            )
        )
    ) is not None
    class_roles = set(
        (
            await session.scalars(
                select(ClassMembership.role).where(
                    ClassMembership.user_id == user.id,
                    ClassMembership.is_active.is_(True),
                )
            )
        ).all()
    )
    class_manager = Role.CLASS_MANAGER in class_roles
    approver = school_admin or class_manager or Role.TRUSTED_APPROVER in class_roles
    return main_reply_keyboard(
        is_platform_admin=platform_admin,
        is_school_admin=school_admin,
        is_class_manager=class_manager,
        is_approver=approver,
    )


async def _classes_for_user(session: AsyncSession, user: User) -> list[ClassGroup]:
    query = (
        select(ClassGroup)
        .join(AcademicYear, ClassGroup.academic_year_id == AcademicYear.id)
        .outerjoin(
            ClassMembership,
            (ClassMembership.class_id == ClassGroup.id)
            & (ClassMembership.user_id == user.id)
            & ClassMembership.is_active.is_(True),
        )
        .outerjoin(
            SchoolMembership,
            (SchoolMembership.school_id == AcademicYear.school_id)
            & (SchoolMembership.user_id == user.id)
            & (SchoolMembership.role == Role.SCHOOL_ADMIN)
            & SchoolMembership.is_active.is_(True),
        )
        .where(
            ClassGroup.is_active.is_(True),
            or_(
                ClassMembership.id.is_not(None),
                SchoolMembership.id.is_not(None),
                user.is_platform_admin,
            ),
        )
        .distinct()
        .order_by(ClassGroup.name)
    )
    return list((await session.execute(query)).scalars().all())


async def _class_from_message(
    session: AsyncSession,
    message: Message,
    class_id: int | None = None,
    user: User | None = None,
):
    if message.chat.type in {"group", "supergroup"}:
        return await get_group_context(session, message.chat.id)
    if class_id is None or user is None:
        return None
    context = await get_class_with_school(session, class_id)
    if context is None or not await user_can_access_class(session, user, class_id):
        return None
    return context


async def _ensure_group_member(
    session: AsyncSession, user: User, class_id: int
) -> None:
    membership = await session.scalar(
        select(ClassMembership).where(
            ClassMembership.class_id == class_id,
            ClassMembership.user_id == user.id,
        )
    )
    if membership is None:
        session.add(ClassMembership(class_id=class_id, user_id=user.id, role=Role.STUDENT))
    elif not membership.is_active:
        membership.is_active = True


async def _subject_list(session: AsyncSession, class_id: int) -> list[Subject]:
    return list(
        (
            await session.execute(
                select(Subject)
                .join(ClassSubject, ClassSubject.subject_id == Subject.id)
                .where(
                    ClassSubject.class_id == class_id,
                    ClassSubject.deleted_at.is_(None),
                    Subject.deleted_at.is_(None),
                )
                .order_by(Subject.name)
            )
        ).scalars().all()
    )


def _parse_pipe_args(raw: str, count: int) -> list[str]:
    values = [part.strip() for part in raw.split("|")]
    if len(values) != count or any(not value for value in values):
        raise ValueError(f"تعداد یا قالب ورودی اشتباه است؛ {count} بخش را با | جدا کنید.")
    return values
