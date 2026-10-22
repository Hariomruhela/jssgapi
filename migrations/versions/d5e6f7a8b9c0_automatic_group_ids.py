"""automatic group IDs: state/city columns, unique index, per-city counters

Revision ID: d5e6f7a8b9c0
Revises: a3b4c5d6e7f8
Create Date: 2026-10-15 00:00:00.000000

``POST /api/v1/groups`` now generates a Group ID of its own -
``STATE_CODE + CITY_CODE + SEQUENCE``, e.g. ``MP09001`` for the first Indore
group - so the ID has to be stored and it has to stay unique. Three nullable
columns join ``social_groups``:

* ``group_id`` - the generated business code. Nullable and unindexed-free of
  defaults, so every existing row (sheet/directory imports included) keeps
  working and is **not** backfilled or rewritten.
* ``state`` / ``city`` - the canonical, registry-validated place names the ID
  was built from.

``group_id_counters`` holds one row per city prefix (``MP09``) and is the
concurrency-safe sequence: allocation is a single atomic upsert, never
``COUNT(*) + 1`` and never an in-memory counter.

The unique index on ``group_id`` is the database-level backstop. Postgres
treats NULLs as distinct, so the pre-existing ID-less rows do not collide.
Duplicates are checked before the index is created, so a database that somehow
already holds a repeated ID fails loudly here instead of half-applying.

Every statement is ``IF NOT EXISTS``/guarded, so the revision is safe to re-run
and safe against a partially-migrated database.

Downgrade drops the columns and the counters table - and with them every
stored Group ID. Export ``social_groups`` first if the values matter.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "d5e6f7a8b9c0"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None

COLUMNS = {
    "group_id": "VARCHAR(16)",
    "state": "VARCHAR(100)",
    "city": "VARCHAR(100)",
}

UNIQUE_INDEX = "uq_social_groups_group_id"


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


def _duplicate_group_ids() -> list[str]:
    return [
        row[0]
        for row in op.get_bind()
        .execute(
            text(
                "SELECT group_id FROM social_groups "
                "WHERE group_id IS NOT NULL "
                "GROUP BY group_id HAVING count(*) > 1"
            )
        )
        .all()
    ]


def upgrade() -> None:
    for column, column_type in COLUMNS.items():
        if _column_exists("social_groups", column):
            continue
        op.execute(
            f"ALTER TABLE social_groups ADD COLUMN {column} {column_type}"  # noqa: S608
        )

    # The backstop for concurrent creates. Checked first so a database that
    # already holds duplicates reports the offending values instead of dying
    # on a bare "could not create unique index".
    duplicates = _duplicate_group_ids()
    if duplicates:
        raise RuntimeError(
            "social_groups.group_id already holds duplicate values "
            f"({', '.join(duplicates)}); resolve them before this migration "
            "can enforce uniqueness."
        )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {UNIQUE_INDEX} "  # noqa: S608
        "ON social_groups (group_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS group_id_counters (
            city_key   VARCHAR(16) PRIMARY KEY,
            last_value INTEGER     NOT NULL
        )
        """
    )

    # Seed each counter from the highest ID already stored for that prefix, so
    # re-running the chain against a database that already generated IDs by
    # hand can never reissue a number. No-op while the column is empty.
    op.execute(
        """
        INSERT INTO group_id_counters (city_key, last_value)
        SELECT substring(group_id from '^[A-Z]{2}[0-9]{2}'),
               max(substring(group_id from '^[A-Z]{2}[0-9]{2}([0-9]+)$')::int)
        FROM social_groups
        WHERE group_id ~ '^[A-Z]{2}[0-9]{2}[0-9]+$'
        GROUP BY 1
        ON CONFLICT (city_key) DO UPDATE
            SET last_value = GREATEST(group_id_counters.last_value, EXCLUDED.last_value)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {UNIQUE_INDEX}")  # noqa: S608
    op.execute("DROP TABLE IF EXISTS group_id_counters")
    op.execute("ALTER TABLE social_groups DROP COLUMN IF EXISTS group_id")
    op.execute("ALTER TABLE social_groups DROP COLUMN IF EXISTS state")
    op.execute("ALTER TABLE social_groups DROP COLUMN IF EXISTS city")
