import hashlib
import secrets

from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.domain.enums import Role
from classmate.db.models import (
    AcademicYear,
    ClassGroup,
    ClassMembership,
    School,
    SchoolMembership,
    User,
)


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


def make_code() -> str:
    return secrets.token_urlsafe(8).replace("-", "").replace("_", "").upper()[:10]


async def ensure_user(
    session: AsyncSession, telegram_user: TelegramUser, private_chat_started: bool = False
) -> User:
    user = await session.scalar(
        select(User).where(User.telegram_id == telegram_user.id)
    )
    if user is None:
        user = User(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            full_name=telegram_user.full_name[:160],
            private_chat_started=private_chat_started,
        )
        session.add(user)
    else:
        user.username = telegram_user.username
        user.full_name = telegram_user.full_name[:160]
        if private_chat_started:
            user.private_chat_started = True
    await session.flush()
    return user


async def get_class_with_school(
    session: AsyncSession, class_id: int
) -> tuple[ClassGroup, AcademicYear, School] | None:
    row = (
        await session.execute(
            select(ClassGroup, AcademicYear, School)
            .join(AcademicYear, ClassGroup.academic_year_id == AcademicYear.id)
            .join(School, AcademicYear.school_id == School.id)
            .where(ClassGroup.id == class_id, ClassGroup.is_active.is_(True))
        )
    ).first()
    return row


async def class_membership(
    session: AsyncSession, user_id: int, class_id: int
) -> ClassMembership | None:
    return await session.scalar(
        select(ClassMembership).where(
            ClassMembership.user_id == user_id,
            ClassMembership.class_id == class_id,
            ClassMembership.is_active.is_(True),
        )
    )


async def is_school_admin(
    session: AsyncSession, user: User, school_id: int
) -> bool:
    if user.is_platform_admin:
        return True
    return (
        await session.scalar(
            select(SchoolMembership.id).where(
                SchoolMembership.user_id == user.id,
                SchoolMembership.school_id == school_id,
                SchoolMembership.role == Role.SCHOOL_ADMIN,
                SchoolMembership.is_active.is_(True),
            )
        )
    ) is not None


async def can_approve(
    session: AsyncSession, user: User, class_id: int
) -> bool:
    if user.is_platform_admin:
        return True
    membership = await class_membership(session, user.id, class_id)
    if membership and membership.role in {Role.CLASS_MANAGER, Role.TRUSTED_APPROVER}:
        return True
    school_id = await session.scalar(
        select(AcademicYear.school_id)
        .join(ClassGroup, ClassGroup.academic_year_id == AcademicYear.id)
        .where(ClassGroup.id == class_id)
    )
    if school_id is None:
        return False
    return await is_school_admin(session, user, school_id)


async def user_can_access_class(
    session: AsyncSession, user: User, class_id: int
) -> bool:
    if user.is_platform_admin:
        return True
    if await class_membership(session, user.id, class_id):
        return True
    school_id = await session.scalar(
        select(AcademicYear.school_id)
        .join(ClassGroup, ClassGroup.academic_year_id == AcademicYear.id)
        .where(ClassGroup.id == class_id)
    )
    return bool(school_id and await is_school_admin(session, user, school_id))
