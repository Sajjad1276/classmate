from enum import StrEnum


class Role(StrEnum):
    SCHOOL_ADMIN = "school_admin"
    CLASS_MANAGER = "class_manager"
    TRUSTED_APPROVER = "trusted_approver"
    STUDENT = "student"


class RecordStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class ReminderStatus(StrEnum):
    PENDING = "pending"
    RETRY = "retry"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


class LessonStatus(StrEnum):
    SCHEDULED = "scheduled"


class EntityType(StrEnum):
    HOMEWORK = "homework"
    EXAM = "exam"
    REPORT = "report"


ENTITY_LABELS = {
    EntityType.HOMEWORK: "تکلیف",
    EntityType.EXAM: "امتحان",
    EntityType.REPORT: "گزارش روزانه",
}


class UpdateStatus(StrEnum):
    PROCESSING = "processing"
    FAILED = "failed"
    PROCESSED = "processed"
