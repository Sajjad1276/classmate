from datetime import datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.db.models import (
    ClassGroup,
    ClassMembership,
    Exam,
    Homework,
    NotificationPreference,
    User,
    ReminderJob,
    Subject,
)


async def schedule_approved_record_reminders(
    session: AsyncSession,
    class_group: ClassGroup,
    entity: Homework | Exam,
    entity_type: str,
    school_timezone: str,
    *,
    target_user_id: int | None = None,
    include_group: bool = True,
) -> int:
    """Persist group and opted-in private reminders for an approved record."""

    zone = ZoneInfo(school_timezone)
    now = datetime.now(zone)
    if entity_type == "exam":
        due_at = entity.scheduled_at.astimezone(zone)
        candidates = [
            (due_at - timedelta(hours=24), "۲۴ ساعت تا امتحان"),
            (due_at - timedelta(hours=1), "یک ساعت تا امتحان"),
        ]
        label = "امتحان"
    else:
        if entity.due_on is None:
            return 0
        candidates = [
            (
                datetime.combine(
                    entity.due_on - timedelta(days=1), time(18, 0), tzinfo=zone
                ),
                "یادآوری تکلیف فردا",
            )
        ]
        label = "تکلیف"

    candidates = [(when, prefix) for when, prefix in candidates if when > now]
    if not candidates:
        return 0

    targets: list[tuple[str, int]] = []
    if include_group and class_group.group_chat_id is not None:
        targets.append(("group", class_group.group_chat_id))
    preferences = (
        await session.execute(
            select(User.telegram_id)
            .join(ClassMembership, ClassMembership.user_id == User.id)
            .outerjoin(
                NotificationPreference,
                (NotificationPreference.user_id == User.id)
                & (NotificationPreference.class_id == class_group.id),
            )
            .where(
                ClassMembership.class_id == class_group.id,
                ClassMembership.is_active.is_(True),
                User.private_chat_started.is_(True),
                User.is_active.is_(True),
                *([User.id == target_user_id] if target_user_id is not None else []),
                or_(
                    NotificationPreference.exams_enabled.is_(True)
                    if entity_type == "exam"
                    else NotificationPreference.homework_enabled.is_(True),
                    NotificationPreference.id.is_(None),
                ),
            )
        )
    ).all()
    targets.extend(("private", row[0]) for row in preferences)

    created = 0
    for scheduled_at, prefix in candidates:
        for target_type, target_id in set(targets):
            dedupe = (
                f"{entity_type}:{entity.id}:{target_type}:{target_id}:"
                f"{scheduled_at.isoformat()}"
            )
            existing = await session.scalar(
                select(ReminderJob)
                .where(ReminderJob.dedupe_key == dedupe)
                .with_for_update()
            )
            if existing is not None:
                if existing.status == "cancelled":
                    existing.status = "pending"
                    existing.scheduled_at = scheduled_at
                    existing.attempts = 0
                    existing.last_error = None
                    existing.locked_at = None
                    created += 1
                continue
            subject = await session.get(Subject, entity.subject_id)
            message = (
                f"🔔 <b>{prefix}</b>\n"
                f"درس: {escape(subject.name) if subject else 'نامشخص'}\n"
                f"{label}: {escape(entity.title)}"
            )
            session.add(
                ReminderJob(
                    class_id=class_group.id,
                    entity_type=entity_type,
                    entity_id=entity.id,
                    target_type=target_type,
                    target_id=target_id,
                    message_text=message,
                    scheduled_at=scheduled_at,
                    dedupe_key=dedupe,
                )
            )
            created += 1
    return created
