from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from classmate.db.models import (
    AcademicYear,
    ClassGroup,
    ClassMembership,
    ClassSubject,
    Homework,
    NotificationPreference,
    ReminderJob,
    School,
    Subject,
    User,
)
from classmate.domain.enums import ReminderStatus
from classmate.services.notifications import (
    schedule_approved_record_reminders,
    update_user_reminder_preference,
)


async def _make_target(session, *, has_preference: bool, enabled: bool):
    school = School(name="Reminder School", timezone="Asia/Tehran")
    session.add(school)
    await session.flush()
    year = AcademicYear(school_id=school.id, name="1405-1406")
    session.add(year)
    await session.flush()
    class_group = ClassGroup(academic_year_id=year.id, name="10B", grade="10")
    user = User(telegram_id=920000001, full_name="Student", private_chat_started=True)
    session.add_all([class_group, user])
    await session.flush()
    subject = Subject(school_id=school.id, name="Physics", normalized_name="physics")
    session.add(subject)
    await session.flush()
    session.add(ClassSubject(class_id=class_group.id, subject_id=subject.id))
    session.add(ClassMembership(class_id=class_group.id, user_id=user.id, role="student"))
    if has_preference:
        session.add(NotificationPreference(
            user_id=user.id, class_id=class_group.id,
            homework_enabled=enabled, exams_enabled=True,
        ))
    homework = Homework(
        class_id=class_group.id, subject_id=subject.id, title="Lab report",
        description="Complete pages 1-2", due_on=date.today() + timedelta(days=3),
        created_by=user.id, status="APPROVED",
    )
    session.add(homework)
    await session.flush()
    return class_group, homework, user, school


@pytest.mark.asyncio
async def test_disabled_homework_preference_suppresses_private_reminder(db_session):
    class_group, homework, _, _ = await _make_target(db_session, has_preference=True, enabled=False)
    count = await schedule_approved_record_reminders(
        db_session, class_group, homework, "homework", "Asia/Tehran"
    )
    assert count == 0


@pytest.mark.asyncio
async def test_missing_preference_uses_documented_enabled_default(db_session):
    class_group, homework, _, _ = await _make_target(db_session, has_preference=False, enabled=True)
    count = await schedule_approved_record_reminders(
        db_session, class_group, homework, "homework", "Asia/Tehran"
    )
    assert count == 1


@pytest.mark.asyncio
async def test_turning_off_private_homework_cancels_only_matching_user_jobs(db_session):
    class_group, homework, user, school = await _make_target(
        db_session, has_preference=True, enabled=True
    )
    class_group.group_chat_id = -100111222333
    other_user = User(telegram_id=920000002, full_name="Other student", private_chat_started=True)
    db_session.add(other_user)
    await db_session.flush()
    jobs = [
        ReminderJob(
            class_id=class_group.id, entity_type="homework", entity_id=homework.id,
            target_type="private", target_id=user.telegram_id, message_text="private homework",
            scheduled_at=datetime.now(timezone.utc), dedupe_key="toggle:private:own",
        ),
        ReminderJob(
            class_id=class_group.id, entity_type="homework", entity_id=homework.id,
            target_type="private", target_id=other_user.telegram_id, message_text="other private",
            scheduled_at=datetime.now(timezone.utc), dedupe_key="toggle:private:other",
        ),
        ReminderJob(
            class_id=class_group.id, entity_type="homework", entity_id=homework.id,
            target_type="group", target_id=class_group.group_chat_id, message_text="group",
            scheduled_at=datetime.now(timezone.utc), dedupe_key="toggle:group",
        ),
        ReminderJob(
            class_id=class_group.id, entity_type="exam", entity_id=123,
            target_type="private", target_id=user.telegram_id, message_text="exam",
            scheduled_at=datetime.now(timezone.utc), dedupe_key="toggle:exam",
        ),
    ]
    db_session.add_all(jobs)
    await db_session.flush()

    created, cancelled = await update_user_reminder_preference(
        db_session, user, class_group, school.timezone, "homework", False
    )
    assert created == 0
    assert cancelled == 1
    assert jobs[0].status == ReminderStatus.CANCELLED
    assert jobs[1].status == ReminderStatus.PENDING
    assert jobs[2].status == ReminderStatus.PENDING
    assert jobs[3].status == ReminderStatus.PENDING


@pytest.mark.asyncio
async def test_turning_homework_back_on_reactivates_future_private_reminder(db_session):
    zone = ZoneInfo("Asia/Tehran")
    class_group, homework, user, school = await _make_target(
        db_session, has_preference=True, enabled=False
    )
    scheduled_at = datetime.combine(
        homework.due_on - timedelta(days=1), time(18, 0), tzinfo=zone
    )
    dedupe_key = (
        f"homework:{homework.id}:private:{user.telegram_id}:{scheduled_at.isoformat()}"
    )
    job = ReminderJob(
        class_id=class_group.id, entity_type="homework", entity_id=homework.id,
        target_type="private", target_id=user.telegram_id, message_text="previously disabled",
        scheduled_at=scheduled_at, dedupe_key=dedupe_key, status=ReminderStatus.CANCELLED,
        attempts=2, last_error="cancelled by preference",
    )
    db_session.add(job)
    await db_session.flush()

    created, cancelled = await update_user_reminder_preference(
        db_session, user, class_group, school.timezone, "homework", True
    )
    assert created == 1
    assert cancelled == 0
    assert job.status == ReminderStatus.PENDING
    assert job.attempts == 0
    assert job.last_error is None
    assert job.sent_at is None
    assert job.target_type == "private"


@pytest.mark.asyncio
async def test_reenable_does_not_reopen_sent_failed_or_uncertain_jobs(db_session):
    class_group, homework, user, school = await _make_target(
        db_session, has_preference=True, enabled=False
    )
    scheduled_at = datetime.combine(
        homework.due_on - timedelta(days=1), time(18, 0), tzinfo=ZoneInfo(school.timezone)
    )
    dedupe_key = (
        f"homework:{homework.id}:private:{user.telegram_id}:{scheduled_at.isoformat()}"
    )
    for index, terminal_status in enumerate(
        [ReminderStatus.SENT, ReminderStatus.FAILED, ReminderStatus.UNCERTAIN]
    ):
        # Keep the unique dedupe key stable; update the existing row between attempts.
        if index == 0:
            job = ReminderJob(
                class_id=class_group.id, entity_type="homework", entity_id=homework.id,
                target_type="private", target_id=user.telegram_id, message_text="terminal",
                scheduled_at=scheduled_at, dedupe_key=dedupe_key, status=terminal_status,
            )
            db_session.add(job)
            await db_session.flush()
        else:
            job.status = terminal_status
            await db_session.flush()
        created, cancelled = await update_user_reminder_preference(
            db_session, user, class_group, school.timezone, "homework", True
        )
        assert created == 0
        assert cancelled == 0
        assert job.status == terminal_status


@pytest.mark.asyncio
async def test_reenable_does_not_duplicate_existing_pending_private_reminder(db_session):
    class_group, homework, user, school = await _make_target(
        db_session, has_preference=True, enabled=False
    )
    scheduled_at = datetime.combine(
        homework.due_on - timedelta(days=1), time(18, 0), tzinfo=ZoneInfo(school.timezone)
    )
    dedupe_key = (
        f"homework:{homework.id}:private:{user.telegram_id}:{scheduled_at.isoformat()}"
    )
    job = ReminderJob(
        class_id=class_group.id, entity_type="homework", entity_id=homework.id,
        target_type="private", target_id=user.telegram_id, message_text="already queued",
        scheduled_at=scheduled_at, dedupe_key=dedupe_key, status=ReminderStatus.PENDING,
    )
    db_session.add(job)
    await db_session.flush()

    created, cancelled = await update_user_reminder_preference(
        db_session, user, class_group, school.timezone, "homework", True
    )
    assert created == 0
    assert cancelled == 0
    assert job.status == ReminderStatus.PENDING
    assert job.attempts == 0
