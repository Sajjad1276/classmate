from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.application.access import require_class_access, require_class_role
from classmate.db.models import (
    ClassGroup,
    ClassMembership,
    ClassSubject,
    Subject,
    TimetableEntry,
    User,
)
from classmate.domain.enums import Role
from classmate.domain.errors import NotFound, ValidationError
from classmate.domain.text import normalize_name
from classmate.domain.codes import generate_code, hash_code


async def issue_member_invite(
    session: AsyncSession, actor: User, class_id: int
) -> str:
    await require_class_role(session, actor, class_id, {Role.CLASS_MANAGER})
    class_group = await session.scalar(
        select(ClassGroup).where(ClassGroup.id == class_id).with_for_update()
    )
    if class_group is None:
        raise NotFound("کلاس پیدا نشد.")
    code = generate_code()
    class_group.member_join_code_hash = hash_code(code)
    class_group.member_join_code_expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    await session.flush()
    return code


async def join_class(
    session: AsyncSession, user: User, code: str
) -> ClassGroup:
    class_group = await session.scalar(
        select(ClassGroup)
        .where(
            ClassGroup.member_join_code_hash == hash_code(code),
            ClassGroup.member_join_code_expires_at > datetime.now(timezone.utc),
            ClassGroup.is_active.is_(True),
        )
        .with_for_update()
    )
    if class_group is None:
        raise NotFound("کد دعوت نامعتبر یا منقضی شده است.")
    membership = await session.scalar(
        select(ClassMembership).where(
            ClassMembership.class_id == class_group.id,
            ClassMembership.user_id == user.id,
        )
    )
    if membership is None:
        session.add(
            ClassMembership(
                class_id=class_group.id,
                user_id=user.id,
                role=Role.STUDENT.value,
                is_active=True,
            )
        )
    else:
        membership.is_active = True
    await session.flush()
    return class_group


async def add_subject(
    session: AsyncSession, actor: User, class_id: int, name: str
) -> Subject:
    _, _, school = await require_class_role(
        session, actor, class_id, {Role.CLASS_MANAGER}
    )
    clean_name = normalize_name(name, maximum=100)
    normalized = clean_name.casefold().replace("ي", "ی").replace("ك", "ک")
    subject = await session.scalar(
        select(Subject).where(
            Subject.school_id == school.id,
            Subject.normalized_name == normalized,
        )
    )
    if subject is None:
        subject = Subject(school_id=school.id, name=clean_name, normalized_name=normalized)
        session.add(subject)
        await session.flush()
    link = await session.scalar(
        select(ClassSubject).where(
            ClassSubject.class_id == class_id,
            ClassSubject.subject_id == subject.id,
        )
    )
    if link is None:
        session.add(ClassSubject(class_id=class_id, subject_id=subject.id))
    await session.flush()
    return subject


async def list_subjects(
    session: AsyncSession, actor: User, class_id: int
) -> list[Subject]:
    await require_class_access(session, actor, class_id)
    query = (
        select(Subject)
        .join(ClassSubject, ClassSubject.subject_id == Subject.id)
        .where(ClassSubject.class_id == class_id)
        .order_by(Subject.name)
    )
    return list((await session.scalars(query)).all())


async def set_timetable_slot(
    session: AsyncSession,
    actor: User,
    class_id: int,
    *,
    weekday: int,
    period: int,
    subject_id: int,
    starts_at,
    ends_at,
    valid_from=None,
    valid_until=None,
) -> TimetableEntry:
    await require_class_role(session, actor, class_id, {Role.CLASS_MANAGER})
    if weekday not in range(7) or period not in range(1, 16):
        raise ValidationError("روز یا شماره زنگ معتبر نیست.")
    if starts_at and ends_at and starts_at >= ends_at:
        raise ValidationError("ساعت پایان باید بعد از ساعت شروع باشد.")
    link = await session.scalar(
        select(ClassSubject.id).where(
            ClassSubject.class_id == class_id,
            ClassSubject.subject_id == subject_id,
        )
    )
    if link is None:
        raise ValidationError("این درس به کلاس متصل نیست.")
    slot = await session.scalar(
        select(TimetableEntry).where(
            TimetableEntry.class_id == class_id,
            TimetableEntry.weekday == weekday,
            TimetableEntry.period == period,
        )
    )
    if slot is None:
        slot = TimetableEntry(
            class_id=class_id,
            weekday=weekday,
            period=period,
            subject_id=subject_id,
            starts_at=starts_at,
            ends_at=ends_at,
            valid_from=valid_from,
            valid_until=valid_until,
        )
        session.add(slot)
    else:
        slot.subject_id = subject_id
        slot.starts_at = starts_at
        slot.ends_at = ends_at
        slot.valid_from = valid_from
        slot.valid_until = valid_until
    await session.flush()
    return slot



async def assign_class_manager(
    session: AsyncSession, actor: User, class_id: int, target: User
) -> ClassMembership:
    await require_class_role(session, actor, class_id, {Role.CLASS_MANAGER})
    membership = await session.scalar(
        select(ClassMembership).where(
            ClassMembership.class_id == class_id,
            ClassMembership.user_id == target.id,
        )
    )
    if membership is None:
        membership = ClassMembership(
            class_id=class_id,
            user_id=target.id,
            role=Role.CLASS_MANAGER.value,
            is_active=True,
        )
        session.add(membership)
    else:
        if membership.role == Role.CLASS_MANAGER.value and membership.is_active:
            return membership
        membership.role = Role.CLASS_MANAGER.value
        membership.is_active = True
    await session.flush()
    return membership
