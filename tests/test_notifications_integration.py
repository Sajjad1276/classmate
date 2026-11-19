from datetime import date, timedelta

import pytest

from classmate.db.models import (
    AcademicYear,
    ClassGroup,
    ClassMembership,
    ClassSubject,
    Homework,
    NotificationPreference,
    School,
    Subject,
    User,
)
from classmate.services.notifications import schedule_approved_record_reminders


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
    return class_group, homework


@pytest.mark.asyncio
async def test_disabled_homework_preference_suppresses_private_reminder(db_session):
    class_group, homework = await _make_target(db_session, has_preference=True, enabled=False)
    count = await schedule_approved_record_reminders(
        db_session, class_group, homework, "homework", "Asia/Tehran"
    )
    assert count == 0


@pytest.mark.asyncio
async def test_missing_preference_uses_documented_enabled_default(db_session):
    class_group, homework = await _make_target(db_session, has_preference=False, enabled=True)
    count = await schedule_approved_record_reminders(
        db_session, class_group, homework, "homework", "Asia/Tehran"
    )
    assert count == 1
