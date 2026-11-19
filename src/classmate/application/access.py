from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from classmate.db.models import (
    AcademicYear,
    ClassGroup,
    ClassMembership,
    School,
    SchoolMembership,
    User,
)
from classmate.domain.enums import Role
from classmate.domain.errors import Forbidden, NotFound


async def get_class_scope(
    session: AsyncSession, class_id: int
) -> tuple[ClassGroup, AcademicYear, School]:
    result = (
        await session.execute(
            select(ClassGroup, AcademicYear, School)
            .join(AcademicYear, AcademicYear.id == ClassGroup.academic_year_id)
            .join(School, School.id == AcademicYear.school_id)
            .where(
                ClassGroup.id == class_id,
                ClassGroup.is_active.is_(True),
                School.is_active.is_(True),
            )
        )
    ).first()
    if result is None:
        raise NotFound("کلاس فعال پیدا نشد.")
    return result


async def list_accessible_classes(
    session: AsyncSession, user: User
) -> list[tuple[ClassGroup, str]]:
    membership_access = (
        select(ClassMembership.id)
        .where(
            ClassMembership.class_id == ClassGroup.id,
            ClassMembership.user_id == user.id,
            ClassMembership.is_active.is_(True),
        )
        .exists()
    )
    school_access = (
        select(SchoolMembership.id)
        .where(
            SchoolMembership.school_id == AcademicYear.school_id,
            SchoolMembership.user_id == user.id,
            SchoolMembership.role == Role.SCHOOL_ADMIN.value,
            SchoolMembership.is_active.is_(True),
        )
        .exists()
    )
    query = (
        select(ClassGroup, AcademicYear.name)
        .join(AcademicYear, AcademicYear.id == ClassGroup.academic_year_id)
        .where(
            ClassGroup.is_active.is_(True),
            SchoolAccess := or_(
                membership_access,
                school_access,
                user.is_platform_admin,
            ),
        )
        .order_by(AcademicYear.name.desc(), ClassGroup.name)
    )
    return list((await session.execute(query)).all())


async def school_admin_for(
    session: AsyncSession, user: User, school_id: int
) -> bool:
    if user.is_platform_admin:
        return True
    return (
        await session.scalar(
            select(SchoolMembership.id).where(
                SchoolMembership.user_id == user.id,
                SchoolMembership.school_id == school_id,
                SchoolMembership.role == Role.SCHOOL_ADMIN.value,
                SchoolMembership.is_active.is_(True),
            )
        )
    ) is not None


async def class_role_for(
    session: AsyncSession, user: User, class_id: int
) -> str | None:
    return await session.scalar(
        select(ClassMembership.role).where(
            ClassMembership.user_id == user.id,
            ClassMembership.class_id == class_id,
            ClassMembership.is_active.is_(True),
        )
    )


async def require_class_access(
    session: AsyncSession, user: User, class_id: int
) -> tuple[ClassGroup, AcademicYear, School]:
    scope = await get_class_scope(session, class_id)
    class_group, _, school = scope
    if user.is_platform_admin or await school_admin_for(session, user, school.id):
        return scope
    if await class_role_for(session, user, class_id) is not None:
        return scope
    raise Forbidden("به این کلاس دسترسی نداری.")


async def require_class_role(
    session: AsyncSession,
    user: User,
    class_id: int,
    allowed: set[Role],
) -> tuple[ClassGroup, AcademicYear, School]:
    scope = await get_class_scope(session, class_id)
    _, _, school = scope
    if user.is_platform_admin or await school_admin_for(session, user, school.id):
        return scope
    role = await class_role_for(session, user, class_id)
    if role not in {item.value for item in allowed}:
        raise Forbidden("برای این عملیات مجوز کافی نداری.")
    return scope
