from datetime import datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.domain.enums import EntityType, RecordStatus, ReminderStatus
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
    if entity_type == EntityType.EXAM:
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
    preference_enabled = (
        NotificationPreference.exams_enabled
        if entity_type == EntityType.EXAM
        else NotificationPreference.homework_enabled
    )
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
                or_(preference_enabled.is_(True), NotificationPreference.id.is_(None)),
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
                # Re-enable only jobs cancelled by a category opt-out.
                if existing.status == ReminderStatus.CANCELLED:
                    existing.status = ReminderStatus.PENDING
                    existing.scheduled_at = scheduled_at
                    existing.attempts = 0
                    existing.last_error = None
                    existing.locked_at = None
                    existing.sent_at = None
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


async def update_user_reminder_preference(
    session: AsyncSession,
    user: User,
    class_group: ClassGroup,
    school_timezone: str,
    category: str,
    enabled: bool,
) -> tuple[int, int]:
    """Update a category preference and reconcile only this user's private queue."""
    if category not in {EntityType.HOMEWORK, EntityType.EXAM}:
        raise ValueError("Unsupported reminder category")

    # Serialize first-time preference creation and concurrent toggles for this account.
    locked_user = await session.scalar(
        select(User).where(User.id == user.id).with_for_update()
    )
    if locked_user is None:
        raise ValueError("User no longer exists")
    preference = await session.scalar(
        select(NotificationPreference)
        .where(
            NotificationPreference.user_id == locked_user.id,
            NotificationPreference.class_id == class_group.id,
        )
        .with_for_update()
    )
    if preference is None:
        preference = NotificationPreference(
            user_id=locked_user.id,
            class_id=class_group.id,
        )
        session.add(preference)
    if category == EntityType.HOMEWORK:
        preference.homework_enabled = enabled
    else:
        preference.exams_enabled = enabled
    await session.flush()

    if not enabled:
        jobs = (
            await session.execute(
                select(ReminderJob)
                .where(
                    ReminderJob.class_id == class_group.id,
                    ReminderJob.target_type == "private",
                    ReminderJob.target_id == locked_user.telegram_id,
                    ReminderJob.entity_type == category,
                    ReminderJob.status.in_(
                        [ReminderStatus.PENDING, ReminderStatus.RETRY, ReminderStatus.PROCESSING]
                    ),
                )
                .with_for_update()
            )
        ).scalars().all()
        cancelled = 0
        for job in jobs:
            job.status = ReminderStatus.CANCELLED
            job.locked_at = None
            cancelled += 1
        return 0, cancelled

    if category == EntityType.HOMEWORK:
        local_today = datetime.now(ZoneInfo(school_timezone)).date()
        upcoming = (
            await session.scalars(
                select(Homework).where(
                    Homework.class_id == class_group.id,
                    Homework.status == RecordStatus.APPROVED,
                    Homework.due_on.is_not(None),
                    Homework.due_on >= local_today,
                )
            )
        ).all()
    else:
        upcoming = (
            await session.scalars(
                select(Exam).where(
                    Exam.class_id == class_group.id,
                    Exam.status == RecordStatus.APPROVED,
                    Exam.scheduled_at >= datetime.now(timezone.utc),
                )
            )
        ).all()

    created = 0
    for record in upcoming:
        created += await schedule_approved_record_reminders(
            session,
            class_group,
            record,
            category,
            school_timezone,
            target_user_id=locked_user.id,
            include_group=False,
        )
    return created, 0
