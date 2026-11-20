from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

from classmate.db.models import (
    AcademicYear,
    ClassGroup,
    ClassMembership,
    ClassSubject,
    ReminderJob,
    School,
    SchoolMembership,
    Subject,
    TimetableEntry,
    User,
)
from classmate.domain.enums import RecordStatus, ReminderStatus, Role
from classmate.services.permissions import can_approve, can_manage_class, is_school_admin, user_can_access_class
from classmate.services.records import (
    add_subject,
    approve_record,
    cancel_approved_record,
    create_homework,
    create_daily_report,
    get_subject,
    list_reports,
    reject_record,
    soft_delete_subject,
    restore_subject,
    soft_delete_class_subject,
    soft_delete_timetable_entry,
)


async def _seed_class(session):
    school = School(name="Integration School", timezone="Asia/Tehran")
    session.add(school)
    await session.flush()
    year = AcademicYear(school_id=school.id, name="1405-1406", is_current=True)
    session.add(year)
    await session.flush()
    class_group = ClassGroup(
        academic_year_id=year.id, name="Class 10A", grade="10", section="A"
    )
    session.add(class_group)
    first = User(telegram_id=910000001, full_name="Creator")
    approver = User(telegram_id=910000002, full_name="Approver")
    session.add_all([first, approver])
    await session.flush()
    session.add_all([
        ClassMembership(class_id=class_group.id, user_id=first.id, role=Role.STUDENT),
        ClassMembership(class_id=class_group.id, user_id=approver.id, role=Role.TRUSTED_APPROVER),
    ])
    subject = Subject(school_id=school.id, name="Mathematics", normalized_name="mathematics")
    session.add(subject)
    await session.flush()
    session.add(ClassSubject(class_id=class_group.id, subject_id=subject.id))
    await session.flush()
    return school, class_group, first, approver, subject


@pytest.mark.asyncio
async def test_approval_self_approval_guard_and_cancellation(db_session):
    school, class_group, creator, approver, subject = await _seed_class(db_session)
    homework = await create_homework(
        db_session, class_group.id, subject, "Algebra", "Exercises 1-5",
        date.today() + timedelta(days=3), creator.id,
    )

    allowed, message = await approve_record(
        db_session, "homework", homework.id, creator.id,
        school.id, class_group.id, school.timezone,
    )
    assert not allowed
    assert "خودش" in message

    allowed, _ = await approve_record(
        db_session, "homework", homework.id, approver.id,
        school.id, class_group.id, school.timezone,
    )
    assert allowed
    assert homework.status == RecordStatus.APPROVED

    db_session.add_all([
        ReminderJob(
            class_id=class_group.id,
            entity_type="homework",
            entity_id=homework.id,
            target_type="private",
            target_id=creator.telegram_id,
            message_text="Pending reminder",
            scheduled_at=datetime.now(timezone.utc) + timedelta(days=1),
            dedupe_key="integration:cancel:homework:pending",
            status=ReminderStatus.PENDING,
        ),
        ReminderJob(
            class_id=class_group.id,
            entity_type="homework",
            entity_id=homework.id,
            target_type="private",
            target_id=creator.telegram_id,
            message_text="Uncertain reminder",
            scheduled_at=datetime.now(timezone.utc) + timedelta(days=1),
            dedupe_key="integration:cancel:homework:uncertain",
            status=ReminderStatus.UNCERTAIN,
        ),
    ])
    await db_session.flush()

    cancelled, message = await cancel_approved_record(
        db_session, "homework", homework.id, approver.id, school.id, class_group.id
    )
    assert cancelled
    assert "غیرفعال" in message
    assert homework.status == RecordStatus.CANCELLED
    assert homework.cancelled_at is not None
    pending_reminder = await db_session.scalar(select(ReminderJob).where(ReminderJob.dedupe_key == "integration:cancel:homework:pending"))
    uncertain_reminder = await db_session.scalar(select(ReminderJob).where(ReminderJob.dedupe_key == "integration:cancel:homework:uncertain"))
    assert pending_reminder.status == ReminderStatus.CANCELLED
    assert uncertain_reminder.status == ReminderStatus.CANCELLED


@pytest.mark.asyncio
async def test_revision_requested_is_not_an_unreachable_reject_state(db_session):
    school, class_group, creator, approver, subject = await _seed_class(db_session)
    homework = await create_homework(
        db_session, class_group.id, subject, "Geometry", "Proof", None, creator.id
    )
    homework.status = "REVISION_REQUESTED"
    allowed, message = await reject_record(
        db_session, "homework", homework.id, approver.id,
        school.id, class_group.id, "Please fix",
    )
    assert not allowed
    assert homework.status == "REVISION_REQUESTED"
    assert "قابل رد نیست" in message


@pytest.mark.asyncio
async def test_soft_delete_subject_cascades_to_links_and_can_be_restored(db_session):
    school, class_group, creator, approver, subject = await _seed_class(db_session)
    first_entry = TimetableEntry(
        class_id=class_group.id, subject_id=subject.id, weekday=0,
        period=1, starts_at=time(8, 0), ends_at=time(8, 45),
    )
    db_session.add(first_entry)
    await db_session.flush()

    # One class link is removed before the school-wide archive.
    assert await soft_delete_class_subject(db_session, class_group.id, subject.id)
    first_link = await db_session.scalar(
        select(ClassSubject).where(ClassSubject.class_id == class_group.id)
    )
    class_level_deleted_at = first_link.deleted_at
    assert class_level_deleted_at is not None
    assert first_entry.deleted_at is not None

    # A second class still has an active link and timetable slot for the same shared subject.
    second_class = ClassGroup(
        academic_year_id=class_group.academic_year_id,
        name="Class 10B",
        grade="10",
        section="B",
    )
    db_session.add(second_class)
    await db_session.flush()
    second_link = ClassSubject(class_id=second_class.id, subject_id=subject.id)
    second_entry = TimetableEntry(
        class_id=second_class.id, subject_id=subject.id, weekday=1,
        period=2, starts_at=time(9, 0), ends_at=time(9, 45),
    )
    db_session.add_all([second_link, second_entry])
    await db_session.flush()

    assert await soft_delete_subject(db_session, subject.id)
    assert subject.deleted_at is not None
    globally_archived_at = subject.deleted_at

    # SQLite may return timezone-naive values for DateTime(timezone=True),
    # so compare the UTC wall-clock representation rather than tzinfo metadata.
    def same_utc_instant(left, right):
        return left.replace(tzinfo=None) == right.replace(tzinfo=None)

    # Global archive touches only the still-active rows; earlier class-level removals stay intact.
    assert same_utc_instant(first_link.deleted_at, class_level_deleted_at)
    assert same_utc_instant(first_entry.deleted_at, class_level_deleted_at)
    assert same_utc_instant(second_link.deleted_at, globally_archived_at)
    assert same_utc_instant(second_entry.deleted_at, globally_archived_at)
    assert await get_subject(db_session, class_group.id, school.id, "Mathematics") is None
    assert await get_subject(db_session, second_class.id, school.id, "Mathematics") is None

    with pytest.raises(ValueError, match="آرشیو شده"):
        await add_subject(db_session, second_class.id, school.id, "Mathematics")

    assert await restore_subject(db_session, subject.id)
    assert subject.deleted_at is None
    assert same_utc_instant(first_link.deleted_at, class_level_deleted_at)
    assert same_utc_instant(first_entry.deleted_at, class_level_deleted_at)
    assert second_link.deleted_at is None
    assert second_entry.deleted_at is None
    assert await get_subject(db_session, class_group.id, school.id, "Mathematics") is None
    assert await get_subject(db_session, second_class.id, school.id, "Mathematics") is subject

    # A class manager can re-add the subject only after an administrator restores it globally.
    restored = await add_subject(db_session, class_group.id, school.id, "Mathematics")
    assert restored.id == subject.id
    assert first_link.deleted_at is None


@pytest.mark.asyncio
async def test_role_permissions_for_trusted_approver_and_school_admin(db_session):
    school, class_group, creator, approver, subject = await _seed_class(db_session)
    assert await can_approve(db_session, approver, class_group.id)
    assert not await can_manage_class(db_session, approver, class_group.id)
    assert await user_can_access_class(db_session, approver, class_group.id)
    manager = User(telegram_id=910000004, full_name="Class Manager")
    db_session.add(manager)
    await db_session.flush()
    db_session.add(ClassMembership(class_id=class_group.id, user_id=manager.id, role=Role.CLASS_MANAGER))
    await db_session.flush()
    assert await can_manage_class(db_session, manager, class_group.id)
    school_admin = User(telegram_id=910000003, full_name="School Admin")
    db_session.add(school_admin)
    await db_session.flush()
    db_session.add(SchoolMembership(school_id=school.id, user_id=school_admin.id, role=Role.SCHOOL_ADMIN))
    await db_session.flush()
    assert await is_school_admin(db_session, school_admin, school.id)
    assert await can_manage_class(db_session, school_admin, class_group.id)
    assert await user_can_access_class(db_session, school_admin, class_group.id)


@pytest.mark.asyncio
async def test_soft_delete_class_subject_hides_subject_and_timetable_slot(db_session):
    school, class_group, creator, approver, subject = await _seed_class(db_session)
    entry = TimetableEntry(
        class_id=class_group.id, subject_id=subject.id, weekday=1,
        period=2, starts_at=time(9, 0), ends_at=time(9, 45),
    )
    db_session.add(entry)
    await db_session.flush()

    assert await soft_delete_class_subject(db_session, class_group.id, subject.id)
    link = await db_session.scalar(
        select(ClassSubject).where(ClassSubject.class_id == class_group.id)
    )
    assert link.deleted_at is not None
    assert entry.deleted_at is not None
    assert await get_subject(db_session, class_group.id, school.id, "Mathematics") is None


@pytest.mark.asyncio
async def test_soft_delete_timetable_entry_is_idempotent(db_session):
    school, class_group, creator, approver, subject = await _seed_class(db_session)
    entry = TimetableEntry(
        class_id=class_group.id, subject_id=subject.id, weekday=1,
        period=3, starts_at=time(10, 0), ends_at=time(10, 45),
    )
    db_session.add(entry)
    await db_session.flush()

    assert await soft_delete_timetable_entry(db_session, entry.id)
    assert entry.deleted_at is not None
    assert not await soft_delete_timetable_entry(db_session, entry.id)


@pytest.mark.asyncio
async def test_cancelling_approved_daily_report_persists_cancelled_at(db_session):
    school, class_group, creator, approver, subject = await _seed_class(db_session)
    report = await create_daily_report(
        db_session, class_group.id, date.today(), "Test report", creator.id, "Today"
    )
    report.status = RecordStatus.APPROVED
    success, message = await cancel_approved_record(
        db_session, "report", report.id, approver.id, school.id, class_group.id
    )
    assert success
    assert report.status == RecordStatus.CANCELLED
    assert report.cancelled_at is not None


@pytest.mark.asyncio
async def test_multiple_daily_reports_can_be_submitted_for_one_class_date(db_session):
    school, class_group, creator, approver, subject = await _seed_class(db_session)
    report_day = date.today()
    first = await create_daily_report(
        db_session, class_group.id, report_day, "گزارش زنگ اول", creator.id, "گزارش اول"
    )
    second = await create_daily_report(
        db_session, class_group.id, report_day, "گزارش زنگ دوم", approver.id, "گزارش دوم"
    )
    first.status = RecordStatus.APPROVED
    second.status = RecordStatus.APPROVED
    results = await list_reports(db_session, class_group.id, report_day)
    assert {item.id for item in results} == {first.id, second.id}
    assert {item.body for item in results} == {"گزارش زنگ اول", "گزارش زنگ دوم"}
