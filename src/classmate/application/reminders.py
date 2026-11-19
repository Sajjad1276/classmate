from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.db.models import (
    ClassGroup,
    ClassMembership,
    Exam,
    Homework,
    NotificationPreference,
    ReminderJob,
    User,
)
from classmate.domain.enums import RecordStatus, RecordType
from classmate.domain.errors import ValidationError


async def schedule_record(
    session: AsyncSession,
    *,
    class_group: ClassGroup,
    record_type: RecordType,
    record: Homework | Exam,
    timezone_name: str,
    target_user_id: int | None = None,
    include_group: bool = True,
) -> int:
    if record.status != RecordStatus.APPROVED.value:
        return 0
    zone = ZoneInfo(timezone_name)
    if record_type == RecordType.HOMEWORK:
        if record.due_on is None:
            return 0
        due_local = datetime.combine(record.due_on, time(18, 0), tzinfo=zone)
        schedule = [due_local - timedelta(days=1)]
    elif record_type == RecordType.EXAM:
        if record.scheduled_at.tzinfo is None:
            raise ValidationError("زمان امتحان باید منطقه زمانی داشته باشد.")
        due_local = record.scheduled_at.astimezone(zone)
        schedule = [
            due_local - timedelta(hours=24),
            due_local - timedelta(hours=1),
        ]
    else:
        return 0

    now = datetime.now(timezone.utc)
    targets: list[tuple[str, int]] = []
    if include_group and class_group.group_chat_id is not None:
        targets.append(("group", class_group.group_chat_id))
    query = (
        select(User, ClassMembership)
        .join(ClassMembership, ClassMembership.user_id == User.id)
        .where(
            ClassMembership.class_id == class_group.id,
            ClassMembership.is_active.is_(True),
            User.is_active.is_(True),
            User.private_chat_started.is_(True),
        )
    )
    if target_user_id is not None:
        query = query.where(User.id == target_user_id)
    prefs = {
        pref.user_id: pref
        for pref in (
            await session.scalars(
                select(NotificationPreference).where(
                    NotificationPreference.class_id == class_group.id
                )
            )
        ).all()
    }
    users = list((await session.execute(query)).all())
    for user, _ in users:
        pref = prefs.get(user.id)
        enabled = (
            pref.homework_enabled if record_type == RecordType.HOMEWORK
            else pref.exams_enabled if record_type == RecordType.EXAM
            else True
        )
        if enabled:
            targets.append(("private", user.telegram_id))

    message_text = (
        f"🔔 یادآوری تکلیف: {record.title}"
        if record_type == RecordType.HOMEWORK
        else f"🔔 یادآوری امتحان: {record.title}"
    )
    created = 0
    for target_type, target_id in targets:
        for scheduled_at in schedule:
            if scheduled_at <= now:
                continue
            dedupe_key = (
                f"{record_type.value}:{record.id}:{target_type}:"
                f"{target_id}:{scheduled_at.isoformat()}"
            )[:180]
            statement = pg_insert(ReminderJob).values(
                class_id=class_group.id,
                entity_type=record_type.value,
                entity_id=record.id,
                target_type=target_type,
                target_id=target_id,
                message_text=message_text,
                scheduled_at=scheduled_at,
                dedupe_key=dedupe_key,
            )
            inserted = await session.scalar(
                statement.on_conflict_do_nothing(
                    constraint="uq_reminder_dedupe_key"
                ).returning(ReminderJob.id)
            )
            if inserted is not None:
                created += 1
    return created


async def set_preference(
    session: AsyncSession,
    *,
    user: User,
    class_group: ClassGroup,
    category: str,
    enabled: bool,
    timezone_name: str,
) -> tuple[int, int]:
    if category not in {"homework", "exams"}:
        raise ValidationError("نوع یادآوری نامعتبر است.")
    values = {
        "user_id": user.id,
        "class_id": class_group.id,
        "homework_enabled": enabled if category == "homework" else True,
        "exams_enabled": enabled if category == "exams" else True,
    }
    update_values = {
        "homework_enabled" if category == "homework" else "exams_enabled": enabled
    }
    statement = pg_insert(NotificationPreference).values(**values)
    await session.execute(
        statement.on_conflict_do_update(
            constraint="uq_notification_user_class",
            set_=update_values,
        )
    )
    if not enabled:
        entity_type = (
            RecordType.HOMEWORK.value if category == "homework"
            else RecordType.EXAM.value
        )
        result = await session.execute(
            update(ReminderJob)
            .where(
                ReminderJob.class_id == class_group.id,
                ReminderJob.target_type == "private",
                ReminderJob.target_id == user.telegram_id,
                ReminderJob.entity_type == entity_type,
                ReminderJob.status.in_(["pending", "retry", "processing"]),
            )
            .values(status="cancelled", locked_at=None)
        )
        return 0, result.rowcount or 0

    if category == "homework":
        records = (
            await session.scalars(
                select(Homework).where(
                    Homework.class_id == class_group.id,
                    Homework.status == RecordStatus.APPROVED.value,
                    Homework.due_on >= datetime.now(ZoneInfo(timezone_name)).date(),
                )
            )
        ).all()
        kind = RecordType.HOMEWORK
    else:
        records = (
            await session.scalars(
                select(Exam).where(
                    Exam.class_id == class_group.id,
                    Exam.status == RecordStatus.APPROVED.value,
                    Exam.scheduled_at >= datetime.now(timezone.utc),
                )
            )
        ).all()
        kind = RecordType.EXAM

    created = 0
    for record in records:
        created += await schedule_record(
            session,
            class_group=class_group,
            record_type=kind,
            record=record,
            timezone_name=timezone_name,
            target_user_id=user.id,
            include_group=False,
        )
    return created, 0
