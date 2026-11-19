"""Add one Telegram group association per school."""

import sqlalchemy as sa
from alembic import op

revision = "0002_school_group"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _existing_columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _existing_indexes(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    # The initial migration creates tables from current metadata on fresh databases.
    # Guard each change so this revision also upgrades installations where 0001
    # was already applied before school-group fields existed.
    columns = _existing_columns("schools")
    if "telegram_group_id" not in columns:
        op.add_column("schools", sa.Column("telegram_group_id", sa.BigInteger(), nullable=True))
    if "telegram_group_title" not in columns:
        op.add_column("schools", sa.Column("telegram_group_title", sa.String(length=180), nullable=True))
    if "group_link_token_hash" not in columns:
        op.add_column("schools", sa.Column("group_link_token_hash", sa.String(length=64), nullable=True))
    if "group_link_token_expires_at" not in columns:
        op.add_column(
            "schools",
            sa.Column("group_link_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        )

    indexes = _existing_indexes("schools")
    if "ix_schools_telegram_group_id" not in indexes:
        op.create_index("ix_schools_telegram_group_id", "schools", ["telegram_group_id"], unique=True)
    if "ix_schools_group_link_token_hash" not in indexes:
        op.create_index(
            "ix_schools_group_link_token_hash",
            "schools",
            ["group_link_token_hash"],
            unique=True,
        )


def downgrade() -> None:
    indexes = _existing_indexes("schools")
    if "ix_schools_group_link_token_hash" in indexes:
        op.drop_index("ix_schools_group_link_token_hash", table_name="schools")
    if "ix_schools_telegram_group_id" in indexes:
        op.drop_index("ix_schools_telegram_group_id", table_name="schools")

    columns = _existing_columns("schools")
    for column_name in (
        "group_link_token_expires_at",
        "group_link_token_hash",
        "telegram_group_title",
        "telegram_group_id",
    ):
        if column_name in columns:
            op.drop_column("schools", column_name)
