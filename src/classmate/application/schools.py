from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.application.access import school_admin_for
from classmate.db.models import AcademicYear, ClassGroup, School, SchoolMembership, User
from classmate.domain.errors import Conflict, Forbidden, NotFound, ValidationError
from classmate.domain.text import normalize_name


async def create_school(
    session: AsyncSession,
    actor: User,
    *,
    name: str,
    academic_year: str,
    timezone_name: str,
) -> tuple[School, AcademicYear]:
    if not actor.is_platform_admin:
        raise Forbidden("فقط مدیر کل می‌تواند مدرسه ایجاد کند.")
    clean_name = normalize_name(name, maximum=180)
    clean_year = normalize_name(academic_year, minimum=4, maximum=40)
    if len(timezone_name) > 64:
        raise ValidationError("منطقه زمانی معتبر نیست.")
    school = School(name=clean_name, timezone=timezone_name, is_active=True)
    session.add(school)
    await session.flush()
    year = AcademicYear(school_id=school.id, name=clean_year, is_current=True)
    session.add(year)
    session.add(
        SchoolMembership(
            school_id=school.id,
            user_id=actor.id,
            role="school_admin",
            is_active=True,
        )
    )
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict("مدرسه یا سال تحصیلی با این مشخصات از قبل ثبت شده است.") from exc
    return school, year


async def create_academic_year(
    session: AsyncSession, actor: User, *, school_id: int, name: str
) -> AcademicYear:
    school = await session.get(School, school_id)
    if school is None or not school.is_active:
        raise NotFound("مدرسه فعال پیدا نشد.")
    if not await school_admin_for(session, actor, school_id):
        raise Forbidden("مدیر این مدرسه نیستی.")
    clean_name = normalize_name(name, minimum=4, maximum=40)
    year = AcademicYear(school_id=school_id, name=clean_name, is_current=True)
    try:
        await session.flush()
        await session.execute(
            update(AcademicYear)
            .where(AcademicYear.school_id == school_id)
            .values(is_current=False)
        )
        session.add(year)
        await session.flush()
    except IntegrityError as exc:
        raise Conflict("این سال تحصیلی قبلاً برای مدرسه ثبت شده است.") from exc
    return year


async def create_class(
    session: AsyncSession,
    actor: User,
    *,
    school_id: int,
    academic_year_id: int,
    name: str,
    grade: str,
    section: str | None = None,
) -> ClassGroup:
    year = await session.scalar(
        select(AcademicYear).where(
            AcademicYear.id == academic_year_id,
            AcademicYear.school_id == school_id,
        )
    )
    if year is None:
        raise NotFound("سال تحصیلی متعلق به این مدرسه نیست.")
    if not await school_admin_for(session, actor, school_id):
        raise Forbidden("مدیر این مدرسه نیستی.")
    item = ClassGroup(
        academic_year_id=academic_year_id,
        name=normalize_name(name, maximum=80),
        grade=normalize_name(grade, maximum=32),
        section=normalize_name(section, maximum=32) if section else None,
        is_active=True,
    )
    session.add(item)
    await session.flush()
    return item


async def assign_school_admin(
    session: AsyncSession, actor: User, *, school_id: int, target: User
) -> SchoolMembership:
    if not actor.is_platform_admin:
        raise Forbidden("فقط مدیر کل می‌تواند مدیر مدرسه تعیین کند.")
    school = await session.get(School, school_id)
    if school is None:
        raise NotFound("مدرسه پیدا نشد.")
    membership = await session.scalar(
        select(SchoolMembership).where(
            SchoolMembership.school_id == school_id,
            SchoolMembership.user_id == target.id,
        )
    )
    if membership is None:
        membership = SchoolMembership(
            school_id=school_id,
            user_id=target.id,
            role="school_admin",
            is_active=True,
        )
        session.add(membership)
    else:
        membership.role = "school_admin"
        membership.is_active = True
    await session.flush()
    return membership
