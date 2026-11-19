from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.application.access import require_class_role
from classmate.db.models import AcademicYear, ClassGroup, School
from classmate.domain.enums import Role
from classmate.domain.errors import Conflict, NotFound
from classmate.services.permissions import hash_code
from classmate.application.classrooms import make_invite_code


async def issue_class_group_link(
    session: AsyncSession, actor: User, class_id: int
) -> str:
    await require_class_role(session, actor, class_id, {Role.CLASS_MANAGER})
    class_group = await session.scalar(
        select(ClassGroup).where(ClassGroup.id == class_id).with_for_update()
    )
    if class_group is None:
        raise NotFound("کلاس پیدا نشد.")
    code = make_invite_code()
    class_group.group_link_code_hash = hash_code(code)
    class_group.group_link_code_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    await session.flush()
    return code


async def bind_class_group(
    session: AsyncSession,
    *,
    code: str,
    chat_id: int,
    chat_title: str,
) -> ClassGroup:
    await session.execute(select(func.pg_advisory_xact_lock(chat_id)))
    class_group = await session.scalar(
        select(ClassGroup)
        .where(
            ClassGroup.group_link_code_hash == hash_code(code),
            ClassGroup.group_link_code_expires_at > datetime.now(timezone.utc),
            ClassGroup.is_active.is_(True),
        )
        .with_for_update()
    )
    if class_group is None:
        raise NotFound("لینک اتصال کلاس نامعتبر یا منقضی شده است.")
    school_binding = await session.scalar(
        select(School.id).where(School.telegram_group_id == chat_id)
    )
    if school_binding is not None:
        raise Conflict("این گروه از قبل به‌عنوان گروه مرکزی مدرسه متصل است.")
    other_class = await session.scalar(
        select(ClassGroup.id).where(
            ClassGroup.group_chat_id == chat_id,
            ClassGroup.id != class_group.id,
        )
    )
    if other_class is not None:
        raise Conflict("این گروه قبلاً به کلاس دیگری متصل شده است.")
    if class_group.group_chat_id not in (None, chat_id):
        raise Conflict("این کلاس از قبل گروه دیگری دارد. ابتدا اتصال قبلی را حذف کن.")
    class_group.group_chat_id = chat_id
    class_group.group_chat_title = (chat_title or "")[:180] or None
    class_group.group_link_code_hash = None
    class_group.group_link_code_expires_at = None
    await session.flush()
    return class_group


async def get_bound_class(
    session: AsyncSession, chat_id: int
) -> tuple[ClassGroup, School] | None:
    row = (
        await session.execute(
            select(ClassGroup, School)
            .join(AcademicYear, AcademicYear.id == ClassGroup.academic_year_id)
            .join(School, School.id == AcademicYear.school_id)
            .where(
                ClassGroup.group_chat_id == chat_id,
                ClassGroup.is_active.is_(True),
                School.is_active.is_(True),
            )
        )
    ).first()
    return row
