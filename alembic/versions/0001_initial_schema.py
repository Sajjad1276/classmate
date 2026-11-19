"""Initial ClassMate multi-school schema."""

from alembic import op

from classmate.db.base import Base
from classmate.db import models  # noqa: F401

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    raise RuntimeError(
        "The initial schema cannot be downgraded automatically because this would drop all ClassMate data. "
        "Restore a database backup or create an explicit, reviewed data migration."
    )
