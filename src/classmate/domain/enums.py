from enum import StrEnum


class Role(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    SCHOOL_ADMIN = "school_admin"
    CLASS_MANAGER = "class_manager"
    TRUSTED_APPROVER = "trusted_approver"
    STUDENT = "student"


class RecordType(StrEnum):
    HOMEWORK = "homework"
    EXAM = "exam"
    REPORT = "report"


class RecordStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class ReminderCategory(StrEnum):
    HOMEWORK = "homework"
    EXAMS = "exams"
