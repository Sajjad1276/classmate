class DomainError(Exception):
    """Base error safe to translate into a user-facing message."""


class ValidationError(DomainError):
    pass


class Forbidden(DomainError):
    pass


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass
