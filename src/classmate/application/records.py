from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.application.access import require_class_access, require_class_role
from classmate.db.models import (
    ApprovalEvent,
    AuditLog,
    ClassSubject,
    DailyReport,
    Exam,
    Homework,
    ReminderJob,
    Subject,
    User,
)
from classmate.domain.enums import RecordStatus, RecordType, Role
from classmate.domain.errors import Forbidden, NotFound, ValidationError
from classmate.domain.text import normalize_body, normalize_name


async def _subject_for_class(
    session: AsyncSession, class_id: int, subject_id: int
) -> Subject:
    subject = await session.scalar(
        select(Subject)
        .join(ClassSubject, ClassSubject.subject_id == Subject.id)
        .where(
            Subject.id == subject_id,
            ClassSubject.class_id == class_id,
        )
    )
    if subject is None:
        raise NotFound("درس متعلق به این کلاس پیدا نشد.")
    return subject


async def create_homework(
    session: AsyncSession,
    actor: User,
    class_id: int,
    *,
    subject_id: int,
    title: str,
    description: str,
    due_on: date | None,
) -> Homework:
    await require_class_access(session, actor, class_id)
    subject = await _subject_for_class(session, class_id, subject_id)
    item = Homework(
        class_id=class_id,
        subject_id=subject.id,
        title=normalize_name(title, maximum=180),
        description=normalize_body(description, maximum=4000),
        due_on=due_on,
        created_by=actor.id,
        status=RecordStatus.PENDING.value,
    )
    session.add(item)
    await session.flush()
    return item


async def create_exam(
    session: AsyncSession,
    actor: User,
    class_id: int,
    *,
    subject_id: int,
    title: str,
    scope: str,
    scheduled_at: datetime,
    exam_type: str = "exam",
) -> Exam:
    await require_class_access(session, actor, class_id)
    subject = await _subject_for_class(session, class_id, subject_id)
    if scheduled_at.tzinfo is None:
        raise ValidationError("زمان امتحان باید منطقه زمانی داشته باشد.")
    if scheduled_at <= datetime.now(scheduled_at.tzinfo):
        raise ValidationError("زمان امتحان باید در آینده باشد.")
    item = Exam(
        class_id=class_id,
        subject_id=subject.id,
        title=normalize_name(title, maximum=180),
        scope=normalize_body(scope, minimum=1, maximum=2000),
        exam_type=exam_type,
        scheduled_at=scheduled_at,
        created_by=actor.id,
        status=RecordStatus.PENDING.value,
    )
    session.add(item)
    await session.flush()
    return item


async def create_daily_report(
    session: AsyncSession,
    actor: User,
    class_id: int,
    *,
    report_date: date,
    title: str,
    body: str,
) -> DailyReport:
    await require_class_access(session, actor, class_id)
    item = DailyReport(
        class_id=class_id,
        report_date=report_date,
        title=normalize_name(title, maximum=180),
        body=normalize_body(body, maximum=4000),
        created_by=actor.id,
        status=RecordStatus.PENDING.value,
    )
    session.add(item)
    await session.flush()
    return item


async def list_homework(
    session: AsyncSession, actor: User, class_id: int, limit: int = 20
) -> list[tuple[Homework, Subject]]:
    await require_class_access(session, actor, class_id)
    query = (
        select(Homework, Subject)
        .join(Subject, Subject.id == Homework.subject_id)
        .where(
            Homework.class_id == class_id,
            Homework.status == RecordStatus.APPROVED.value,
        )
        .order_by(Homework.due_on.asc().nullslast(), Homework.created_at.desc())
        .limit(min(max(limit, 1), 50))
    )
    return list((await session.execute(query)).all())


async def list_exams(
    session: AsyncSession, actor: User, class_id: int, limit: int = 20
) -> list[tuple[Exam, Subject]]:
    query = (
        select(Exam, Subject)
        .join(Subject, Subject.id == Exam.subject_id)
        .where(
            Exam.class_id == class_id,
            Exam.status == RecordStatus.APPROVED.value,
            Exam.scheduled_at >= datetime.now(timezone.utc),
        )
        .order_by(Exam.scheduled_at.asc())
        .limit(min(max(limit, 1), 50))
    )
    await require_class_access(session, actor, class_id)
    return list((await session.execute(query)).all())


async def get_daily_summary(
    session: AsyncSession, actor: User, class_id: int, local_date: date, tz_name: str
) -> dict[str, list]:
    await require_class_access(session, actor, class_id)
    zone = ZoneInfo(tz_name)
    start = datetime.combine(local_date, time.min, tzinfo=zone)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone)
    reports = list(
        (
            await session.scalars(
                select(DailyReport)
                .where(
                    DailyReport.class_id == class_id,
                    DailyReport.report_date == local_date,
                    DailyReport.status == RecordStatus.APPROVED.value,
                )
                .order_by(DailyReport.created_at.desc())
                .limit(4)
            )
        ).all()
    )
    homework = list(
        (
            await session.execute(
                select(Homework, Subject)
                .join(Subject, Subject.id == Homework.subject_id)
                .where(
                    Homework.class_id == class_id,
                    Homework.status == RecordStatus.APPROVED.value,
                    Homework.due_on == local_date,
                )
                .order_by(Homework.created_at.desc())
                .limit(4)
            )
        ).all()
    )
    exams = list(
        (
            await session.execute(
                select(Exam, Subject)
                .join(Subject, Subject.id == Exam.subject_id)
                .where(
                    Exam.class_id == class_id,
                    Exam.status == RecordStatus.APPROVED.value,
                    Exam.scheduled_at >= start,
                    Exam.scheduled_at < end,
                )
                .order_by(Exam.scheduled_at.asc())
                .limit(4)
            )
        ).all()
    )
    return {"reports": reports, "homework": homework, "exams": exams}


async def list_pending(
    session: AsyncSession, actor: User, class_id: int, limit: int = 12
) -> list[tuple[RecordType, object]]:
    _, _, school = await require_class_access(session, actor, class_id)
    if not await can_review(session, actor, class_id, school.id):
        raise Forbidden("برای بررسی این کلاس مجوز نداری.")
    pending: list[tuple[RecordType, object]] = []
    remaining = min(max(limit, 1), 30)
    for kind, model in (
        (RecordType.HOMEWORK, Homework),
        (RecordType.EXAM, Exam),
        (RecordType.REPORT, DailyReport),
    ):
        records = list(
            (
                await session.scalars(
                    select(model)
                    .where(
                        model.class_id == class_id,
                        model.status == RecordStatus.PENDING.value,
                    )
                    .order_by(model.created_at.asc())
                    .limit(remaining)
                )
            ).all()
        )
        pending.extend((kind, record) for record in records)
        remaining -= len(records)
        if remaining <= 0:
            break
    return pending


async def can_review(
    session: AsyncSession, actor: User, class_id: int, school_id: int
) -> bool:
    from classmate.application.access import class_role_for, school_admin_for

    if actor.is_platform_admin or await school_admin_for(session, actor, school_id):
        return True
    return await class_role_for(session, actor, class_id) in {
        Role.CLASS_MANAGER.value,
        Role.TRUSTED_APPROVER.value,
    }


async def review_record(
    session: AsyncSession,
    actor: User,
    class_id: int,
    record_type: RecordType,
    record_id: int,
    *,
    approve: bool,
    note: str | None = None,
) -> object:
    _, _, school = await require_class_access(session, actor, class_id)
    if not await can_review(session, actor, class_id, school.id):
        raise Forbidden("برای بررسی این کلاس مجوز نداری.")
    model = {
        RecordType.HOMEWORK: Homework,
        RecordType.EXAM: Exam,
        RecordType.REPORT: DailyReport,
    }[record_type]
    record = await session.scalar(
        select(model)
        .where(model.id == record_id, model.class_id == class_id)
        .with_for_update()
    )
    if record is None:
        raise NotFound("درخواست پیدا نشد.")
    if record.status != RecordStatus.PENDING.value:
        raise ValidationError("این درخواست قبلاً بررسی شده است.")
    if approve and record.created_by == actor.id:
        raise Forbidden("ثبت‌کننده نمی‌تواند درخواست خودش را تأیید کند.")
    old_status = record.status
    record.status = (
        RecordStatus.APPROVED.value if approve else RecordStatus.REJECTED.value
    )
    if approve:
        record.approved_by = actor.id
        record.approved_at = datetime.now(timezone.utc)
    session.add(
        ApprovalEvent(
            entity_type=record_type.value,
            entity_id=record_id,
            actor_id=actor.id,
            action=record.status,
            note=normalize_body(note, minimum=1, maximum=1000) if note else None,
        )
    )
    session.add(
        AuditLog(
            school_id=school.id,
            class_id=class_id,
            actor_id=actor.id,
            action="record_approved" if approve else "record_rejected",
            entity_type=record_type.value,
            entity_id=record_id,
            before_data={"status": old_status},
            after_data={"status": record.status},
        )
    )
    await session.flush()
    if approve and record_type in {RecordType.HOMEWORK, RecordType.EXAM}:
        from classmate.application.reminders import schedule_record

        class_group, _, school = await require_class_access(session, actor, class_id)
        await schedule_record(
            session,
            class_group=class_group,
            record_type=record_type,
            record=record,
            timezone_name=school.timezone,
        )
    return record


async def cancel_record(
    session: AsyncSession, actor: User, class_id: int, record_type: RecordType, record_id: int
) -> object:
    await require_class_role(session, actor, class_id, {Role.CLASS_MANAGER})
    model = {RecordType.HOMEWORK: Homework, RecordType.EXAM: Exam}.get(record_type)
    if model is None:
        raise ValidationError("فقط تکلیف یا امتحان قابل لغو است.")
    record = await session.scalar(
        select(model)
        .where(model.id == record_id, model.class_id == class_id)
        .with_for_update()
    )
    if record is None or record.status != RecordStatus.APPROVED.value:
        raise NotFound("رکورد فعال و تأییدشده پیدا نشد.")
    old_status = record.status
    record.status = RecordStatus.CANCELLED.value
    record.cancelled_at = datetime.now(timezone.utc)
    await session.execute(
        update(ReminderJob)
        .where(
            ReminderJob.entity_type == record_type.value,
            ReminderJob.entity_id == record_id,
            ReminderJob.status.in_(["pending", "retry", "processing"]),
        )
        .values(status="cancelled", locked_at=None)
    )
    _, _, school = await require_class_access(session, actor, class_id)
    session.add(
        AuditLog(
            school_id=school.id,
            class_id=class_id,
            actor_id=actor.id,
            action="record_cancelled",
            entity_type=record_type.value,
            entity_id=record_id,
            before_data={"status": old_status},
            after_data={"status": record.status},
        )
    )
    await session.flush()
    return record
