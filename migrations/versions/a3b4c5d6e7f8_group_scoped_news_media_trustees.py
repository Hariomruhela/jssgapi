"""give news, media and trustees a group_id, and link trustees to members

Revision ID: a3b4c5d6e7f8
Revises: f2b3c4d5e6f7
Create Date: 2026-10-05 00:00:00.000000

Three tables could not be group-scoped because they had no group to scope by:

* ``news`` and ``media`` had no ``group_id``, so nothing tied an item to a social
  group and a Group Admin could neither filter by group nor be prevented from
  touching another group's content.
* ``trustees`` had a ``user_id`` but no link to the member record, so promoting an
  existing member to the board created a second, unlinked copy of their identity.

``trustees.member_id`` is what lets a Super Admin "select a member and make them a
trustee" without retyping their details.

All three columns are nullable and default to NULL, so every existing row keeps
working unchanged. Group-scoped authorization treats NULL as "no group", which for
a GROUP_ADMIN means those rows are not visible to it — that is the safe default,
and a Super Admin assigns the items to a group afterwards. Nothing is backfilled,
rewritten or deleted.

Idempotent, so it is safe to run against a partially-migrated database.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "a3b4c5d6e7f8"
down_revision = "f2b3c4d5e6f7"
branch_labels = None
depends_on = None

# Matches the naming convention in ``app/models/base.py`` so ``schema_guard`` and
# this migration agree about what a migrated database looks like.
GROUP_FKS = {
    "news": "fk_news_group_id_social_groups",
    "media": "fk_media_group_id_social_groups",
    "trustees": "fk_trustees_member_id_members",
}

GROUP_COLUMNS = ("news", "media")
MEMBER_COLUMNS = ("trustees",)


def _column_exists(table: str, column: str) -> bool:
    return (
        op.get_bind()
        .execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        )
        .scalar_one_or_none()
        is not None
    )


def _constraint_exists(name: str) -> bool:
    return (
        op.get_bind()
        .execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
            {"name": name},
        )
        .scalar_one_or_none()
        is not None
    )


def upgrade() -> None:
    for table in GROUP_COLUMNS:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS group_id UUID")
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_group_id ON {table} (group_id)"
        )
        if not _constraint_exists(GROUP_FKS[table]):
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {GROUP_FKS[table]} "
                "FOREIGN KEY (group_id) REFERENCES social_groups(id) "
                "ON DELETE CASCADE"
            )

    for table in MEMBER_COLUMNS:
        if _column_exists(table, "member_id"):
            continue
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS member_id UUID")
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_member_id ON {table} (member_id)"
        )
        if not _constraint_exists(GROUP_FKS[table]):
            # ON DELETE SET NULL: retiring a member must not delete the board record.
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {GROUP_FKS[table]} "
                "FOREIGN KEY (member_id) REFERENCES members(id) "
                "ON DELETE SET NULL"
            )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE trustees DROP CONSTRAINT IF EXISTS fk_trustees_member_id_members"
    )
    op.execute("ALTER TABLE trustees DROP COLUMN IF EXISTS member_id")
    for table in GROUP_COLUMNS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {GROUP_FKS[table]}")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS group_id")
