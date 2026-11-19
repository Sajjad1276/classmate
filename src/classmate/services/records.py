from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.db.models import (
    AcademicYear,
    ApprovalEvent,
    AuditLog,
    ClassGroup,
    ClassSubject,
    DailyReport,
    Exam,
    Homework,
    LessonSession,
    ReminderJob,
    School,
    Subject,
    TimetableEntry,
)
from classmate.domain.dates import next_timetable_occurrence
from classmate.domain.enums import LessonStatus, RecordStatus, ReminderStatus


def normalize_subject_name(value: str) -> str:
    return " ".join(
        value.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ").split()
    ).casefold()


async def get_group_context(session: AsyncSession, telegram_chat_id: int):
    return (
        await session.execute(
            select(ClassGroup, AcademicYear, School)
            .join(AcademicYear, ClassGroup.academic_year_id == AcademicYear.id)
            .join(School, AcademicYear.school_id == School.id)
            .where(
                ClassGroup.group_chat_id == telegram_chat_id,
                ClassGroup.is_active.is_(True),
                School.is_active.is_(True),
            )
        )
    ).first()


async def get_subject(
    session: AsyncSession, class_id: int, school_id: int, subject_name: str
) -> Subject | None:
    normalized = normalize_subject_name(subject_name)
    return await session.scalar(
        select(Subject)
        .join(ClassSubject, ClassSubject.subject_id == Subject.id)
        .where(
            ClassSubject.class_id == class_id,
            ClassSubject.deleted_at.is_(None),
            Subject.deleted_at.is_(None),
            Subject.school_id == school_id,
            Subject.normalized_name == normalized,
        )
    )



async def get_or_create_next_lesson(
    session: AsyncSession,
    class_id: int,
    subject_id: int,
    timezone_name: str,
) -> LessonSession | None:
    """Resolve the next future occurrence of a subject from the class timetable."""
    zone = ZoneInfo(timezone_name)
    now = datetime.now(zone)
    entries = (
        await session.execute(
            select(TimetableEntry).where(
                TimetableEntry.class_id == class_id,
                TimetableEntry.subject_id == subject_id,
                TimetableEntry.deleted_at.is_(None),
            )
        )
    ).scalars().all()

    occurrences: list[tuple[datetime, datetime | None]] = []
    for entry in entries:
        occurrence = next_timetable_occurrence(
            now=now,
            weekday=entry.weekday,
            starts_at=entry.starts_at,
            ends_at=entry.ends_at,
            valid_from=entry.valid_from,
            valid_until=entry.valid_until,
        )
        if occurrence is not None:
            occurrences.append(occurrence)

    if not occurrences:
        return None

    starts_at, ends_at = min(occurrences, key=lambda item: item[0])
    existing = await session.scalar(
        select(LessonSession).where(
            LessonSession.class_id == class_id,
            LessonSession.subject_id == subject_id,
            LessonSession.starts_at == starts_at,
        )
    )
    if existing is not None:
        return existing

    lesson = LessonSession(
        class_id=class_id,
        subject_id=subject_id,
        starts_at=starts_at,
        ends_at=ends_at,
        status=LessonStatus.SCHEDULED,
    )
    session.add(lesson)
    await session.flush()
    return lesson


async def list_homework_for_lesson(
    session: AsyncSession, class_id: int, subject_id: int, lesson: LessonSession
) -> list[tuple[Homework, Subject]]:
    return list(
        (
            await session.execute(
                select(Homework, Subject)
                .join(Subject, Homework.subject_id == Subject.id)
                .where(
                    Homework.class_id == class_id,
                    Homework.subject_id == subject_id,
                    Homework.status == RecordStatus.APPROVED,
                    (Homework.due_session_id == lesson.id)
                    | (Homework.due_on == lesson.starts_at.date()),
                )
                .order_by(Homework.created_at.desc())
            )
        ).all()
    )


async def add_subject(
    session: AsyncSession, class_id: int, school_id: int, name: str
) -> Subject:
    normalized = normalize_subject_name(name)
    subject = await session.scalar(
        select(Subject).where(
            Subject.school_id == school_id,
            Subject.normalized_name == normalized,
        )
    )
    if subject is None:
        subject = Subject(school_id=school_id, name=name.strip(), normalized_name=normalized)
        session.add(subject)
        await session.flush()
    elif subject.deleted_at is not None:
        subject.deleted_at = None
    link = await session.scalar(
        select(ClassSubject).where(
            ClassSubject.class_id == class_id, ClassSubject.subject_id == subject.id
        )
    )
    if link is None:
        session.add(ClassSubject(class_id=class_id, subject_id=subject.id))
    elif link.deleted_at is not None:
        link.deleted_at = None
    await session.flush()
    return subject


async def create_homework(
    session: AsyncSession,
    class_id: int,
    subject: Subject,
    title: str,
    description: str,
    due_on: date | None,
    created_by: int,
    due_session_id: int | None = None,
) -> Homework:
    record = Homework(
        class_id=class_id,
        subject_id=subject.id,
        title=title.strip()[:180],
        description=description.strip(),
        due_on=due_on,
        due_session_id=due_session_id,
        created_by=created_by,
        status=RecordStatus.PENDING,
    )
    session.add(record)
    await session.flush()
    return record


async def create_exam(
    session: AsyncSession,
    class_id: int,
    subject: Subject,
    title: str,
    scope: str,
    exam_type: str,
    scheduled_at: datetime,
    created_by: int,
) -> Exam:
    if scheduled_at.tzinfo is None:
        raise ValueError("Exam date/time must be timezone-aware.")
    record = Exam(
        class_id=class_id,
        subject_id=subject.id,
        title=title.strip()[:180],
        scope=scope.strip(),
        exam_type=exam_type,
        scheduled_at=scheduled_at,
        created_by=created_by,
        status=RecordStatus.PENDING,
    )
    session.add(record)
    await session.flush()
    return record


async def create_daily_report(
    session: AsyncSession,
    class_id: int,
    report_date: date,
    body: str,
    created_by: int,
    title: str | None = None,
) -> DailyReport:
    record = DailyReport(
        class_id=class_id,
        report_date=report_date,
        title=(title or "گزارش روزانه")[:180],
        body=body.strip(),
        created_by=created_by,
        status=RecordStatus.PENDING,
    )
    session.add(record)
    await session.flush()
    return record


async def audit(
    session: AsyncSession,
    school_id: int,
    class_id: int | None,
    actor_id: int,
    action: str,
    entity_type: str,
    entity_id: int,
    after_data: dict | None = None,
    before_data: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            school_id=school_id,
            class_id=class_id,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_data=before_data,
            after_data=after_data,
        )
    )


async def approve_record(
    session: AsyncSession,
    entity_type: str,
    entity_id: int,
    actor_id: int,
    school_id: int,
    class_id: int,
    timezone_name: str,
) -> tuple[bool, str]:
    model_map = {"homework": Homework, "exam": Exam, "report": DailyReport}
    model = model_map.get(entity_type)
    if model is None:
        return False, "نوع اطلاعات پشتیبانی نمی‌شود."
    record = await session.get(model, entity_id, with_for_update=True)
    if record is None or record.class_id != class_id:
        return False, "اطلاعات پیدا نشد."
    if record.status != RecordStatus.PENDING:
        return False, f"این مورد در وضعیت {record.status} است و قابل تأیید نیست."
    if record.created_by == actor_id:
        return False, "ثبت‌کننده اجازه تأیید درخواست خودش را ندارد."
    record.status = RecordStatus.APPROVED
    record.approved_by = actor_id
    record.approved_at = datetime.now(ZoneInfo(timezone_name))
    session.add(
        ApprovalEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            actor_id=actor_id,
            action=RecordStatus.APPROVED,
        )
    )
    await audit(
        session, school_id, class_id, actor_id, "approve", entity_type, entity_id,
        {"status": RecordStatus.APPROVED.value},
    )
    await session.flush()
    if entity_type in {"homework", "exam"}:
        from classmate.services.notifications import schedule_approved_record_reminders

        class_group = await session.get(ClassGroup, class_id)
        await schedule_approved_record_reminders(
            session, class_group, record, entity_type, timezone_name
        )
    return True, "اطلاعات تأیید شد."


async def reject_record(
    session: AsyncSession,
    entity_type: str,
    entity_id: int,
    actor_id: int,
    school_id: int,
    class_id: int,
    note: str,
) -> tuple[bool, str]:
    model_map = {"homework": Homework, "exam": Exam, "report": DailyReport}
    model = model_map.get(entity_type)
    if model is None:
        return False, "نوع اطلاعات پشتیبانی نمی‌شود."
    record = await session.get(model, entity_id, with_for_update=True)
    if record is None or record.class_id != class_id:
        return False, "اطلاعات پیدا نشد."
    if record.status != RecordStatus.PENDING:
        return False, "این مورد در وضعیت قابل رد نیست."
    record.status = RecordStatus.REJECTED
    session.add(
        ApprovalEvent(
            entity_type=entity_type, entity_id=entity_id, actor_id=actor_id,
            action=RecordStatus.REJECTED, note=note,
        )
    )
    await audit(
        session, school_id, class_id, actor_id, "reject", entity_type, entity_id,
        {"status": RecordStatus.REJECTED.value, "note": note},
    )
    return True, "درخواست رد شد."


async def cancel_approved_record(
    session: AsyncSession,
    entity_type: str,
    entity_id: int,
    actor_id: int,
    school_id: int,
    class_id: int,
) -> tuple[bool, str]:
    """Cancel an approved class record; authorization is enforced by the caller."""
    model_map = {"homework": Homework, "exam": Exam, "report": DailyReport}
    model = model_map.get(entity_type)
    if model is None:
        return False, "نوع اطلاعات پشتیبانی نمی‌شود."
    record = await session.get(model, entity_id, with_for_update=True)
    if record is None or record.class_id != class_id:
        return False, "اطلاعات پیدا نشد."
    if record.status != RecordStatus.APPROVED:
        return False, "فقط اطلاعات تأییدشده قابل لغو است."
    before = {"status": record.status}
    record.status = RecordStatus.CANCELLED
    if hasattr(record, "cancelled_at"):
        record.cancelled_at = datetime.now(timezone.utc)
    session.add(ApprovalEvent(
        entity_type=entity_type, entity_id=entity_id, actor_id=actor_id,
        action=RecordStatus.CANCELLED.value, note="لغو توسط فرد دارای مجوز",
    ))
    await audit(
        session, school_id, class_id, actor_id, "cancel", entity_type, entity_id,
        {"status": RecordStatus.CANCELLED.value}, before,
    )
    await set_reminders_cancelled(session, entity_type, entity_id)
    return True, "اطلاعات لغو شد و یادآوری‌های زمان‌بندی‌شده آن نیز غیرفعال شدند."


async def soft_delete_subject(session: AsyncSession, subject_id: int) -> bool:
    subject = await session.get(Subject, subject_id, with_for_update=True)
    if subject is None or subject.deleted_at is not None:
        return False
    now = datetime.now(timezone.utc)
    subject.deleted_at = now
    links = (await session.execute(select(ClassSubject).where(ClassSubject.subject_id == subject_id))).scalars().all()
    timetable = (await session.execute(select(TimetableEntry).where(TimetableEntry.subject_id == subject_id))).scalars().all()
    for link in links:
        link.deleted_at = now
    for entry in timetable:
        entry.deleted_at = now
    return True


async def soft_delete_class_subject(session: AsyncSession, class_id: int, subject_id: int) -> bool:
    link = await session.scalar(select(ClassSubject).where(
        ClassSubject.class_id == class_id, ClassSubject.subject_id == subject_id
    ).with_for_update())
    if link is None or link.deleted_at is not None:
        return False
    now = datetime.now(timezone.utc)
    link.deleted_at = now
    entries = (await session.execute(select(TimetableEntry).where(
        TimetableEntry.class_id == class_id, TimetableEntry.subject_id == subject_id
    ))).scalars().all()
    for entry in entries:
        entry.deleted_at = now
    return True


async def soft_delete_timetable_entry(session: AsyncSession, entry_id: int) -> bool:
    entry = await session.get(TimetableEntry, entry_id, with_for_update=True)
    if entry is None or entry.deleted_at is not None:
        return False
    entry.deleted_at = datetime.now(timezone.utc)
    return True


async def list_homework(
    session: AsyncSession, class_id: int, subject_name: str | None = None
) -> list[tuple[Homework, Subject]]:
    query = (
        select(Homework, Subject)
        .join(Subject, Homework.subject_id == Subject.id)
        .where(
            Homework.class_id == class_id,
            Homework.status == RecordStatus.APPROVED,
            Subject.deleted_at.is_(None),
        )
        .order_by(Homework.due_on.asc().nullslast(), Homework.created_at.desc())
        .limit(30)
    )
    if subject_name:
        query = query.where(Subject.normalized_name == normalize_subject_name(subject_name))
    return list((await session.execute(query)).all())


async def list_exams(session: AsyncSession, class_id: int) -> list[tuple[Exam, Subject]]:
    return list(
        (
            await session.execute(
                select(Exam, Subject)
                .join(Subject, Exam.subject_id == Subject.id)
                .where(
                    Exam.class_id == class_id,
                    Exam.status == RecordStatus.APPROVED,
                    Subject.deleted_at.is_(None),
                )
                .order_by(Exam.scheduled_at.asc())
                .limit(30)
            )
        ).all()
    )


async def list_reports(session: AsyncSession, class_id: int, report_date: date):
    return list(
        (
            await session.execute(
                select(DailyReport)
                .where(
                    DailyReport.class_id == class_id,
                    DailyReport.report_date == report_date,
                    DailyReport.status == RecordStatus.APPROVED,
                )
                .order_by(DailyReport.created_at.desc())
            )
        ).scalars().all()
    )


async def list_pending_records(session: AsyncSession, class_id: int):
    pending = []
    for label, model in (("homework", Homework), ("exam", Exam), ("report", DailyReport)):
        records = (
            await session.execute(
                select(model).where(model.class_id == class_id, model.status == RecordStatus.PENDING)
                .order_by(model.created_at.asc()).limit(15)
            )
        ).scalars().all()
        pending.extend((label, record) for record in records)
    return pending


async def set_reminders_cancelled(
    session: AsyncSession, entity_type: str, entity_id: int
) -> None:
    jobs = (
        await session.execute(
            select(ReminderJob).where(
                ReminderJob.entity_type == entity_type,
                ReminderJob.entity_id == entity_id,
                ReminderJob.status.in_([ReminderStatus.PENDING, ReminderStatus.RETRY]),
            )
        )
    ).scalars().all()
    for job in jobs:
        job.status = ReminderStatus.CANCELLED
