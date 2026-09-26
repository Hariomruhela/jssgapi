"""repair members column drift

Revision ID: e6f1a2b3c4d5
Revises: c4d8e9f0a1b2
Create Date: 2026-09-26 00:00:00.000000

Production raised::

    asyncpg.exceptions.UndefinedColumnError: column members.profile_data does not exist

on ``GET/PUT /api/v1/profile`` while ``alembic_version`` already reported
``c4d8e9f0a1b2``. That combination means the revision was stamped without its
DDL executing, so ``alembic upgrade head`` was a silent no-op and could never
recover.

This revision is the safety net: it reconciles **every** column the
``Member`` model maps against the live table, not just ``profile_data``, so any
other column that went missing is added in the same pass. Every statement is
idempotent, so it is safe on a healthy database and safe to re-run.

Verify with::

    python -m scripts.drift_check

``NOT NULL`` columns without a server default are added in three steps
(add nullable -> backfill -> enforce) so that the migration also succeeds on a
populated table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e6f1a2b3c4d5"
down_revision = "c4d8e9f0a1b2"
branch_labels = None
depends_on = None

TABLE = "members"

# (column, postgres type, not null, server default, backfill literal)
COLUMNS: tuple[tuple[str, str, bool, str | None, str | None], ...] = (
    ("id", "UUID", True, "gen_random_uuid()", "gen_random_uuid()"),
    ("user_id", "UUID", True, None, "NULL::uuid"),
    ("group_id", "UUID", False, None, None),
    ("location_id", "UUID", False, None, None),
    ("first_name", "VARCHAR(100)", True, None, "''"),
    ("middle_name", "VARCHAR(100)", False, None, None),
    ("last_name", "VARCHAR(100)", True, None, "''"),
    ("gender", "VARCHAR(20)", False, None, None),
    ("date_of_birth", "DATE", False, None, None),
    ("blood_group", "VARCHAR(10)", False, None, None),
    ("profile_photo_url", "TEXT", False, None, None),
    ("profile_data", "JSONB", True, "'{}'::jsonb", "'{}'::jsonb"),
    ("contact_email", "VARCHAR(255)", False, None, None),
    ("contact_phone", "VARCHAR(20)", False, None, None),
    ("address_line", "TEXT", False, None, None),
    ("occupation_summary", "TEXT", False, None, None),
    ("membership_number", "VARCHAR(50)", False, None, None),
    ("membership_status", "VARCHAR(20)", True, None, "'pending'"),
    ("joined_at", "TIMESTAMPTZ", False, None, None),
    ("approved_at", "TIMESTAMPTZ", False, None, None),
    ("approved_by", "UUID", False, None, None),
    ("rejection_reason", "TEXT", False, None, None),
    ("is_profile_complete", "BOOLEAN", True, "false", "false"),
    ("created_at", "TIMESTAMPTZ", True, "now()", "now()"),
    ("updated_at", "TIMESTAMPTZ", True, "now()", "now()"),
    ("created_by", "UUID", False, None, None),
    ("updated_by", "UUID", False, None, None),
    ("is_deleted", "BOOLEAN", True, "false", "false"),
    ("deleted_at", "TIMESTAMPTZ", False, None, None),
)

# (index name, columns, unique). Dropping a column drops its index with it, so
# these are reconciled in the same pass.
INDEXES: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("ix_members_group_id", ("group_id",), False),
    ("ix_members_location_id", ("location_id",), False),
    ("ix_members_is_deleted", ("is_deleted",), False),
    ("ix_members_membership_number", ("membership_number",), True),
    ("ix_members_membership_status", ("membership_status",), False),
    ("uq_members_user_id", ("user_id",), True),
)


def _column_names() -> set[str]:
    rows = op.get_bind().execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table"
        ),
        {"table": TABLE},
    )
    return {row[0] for row in rows}


def upgrade() -> None:
    if not op.get_bind().dialect.has_table(op.get_bind(), TABLE, schema="public"):
        # The table itself is missing: let the operator reconcile the whole
        # schema with `python -m scripts.drift_check` instead of guessing here.
        raise RuntimeError(
            f"table '{TABLE}' does not exist - run "
            "`python -m scripts.drift_check` and reconcile the schema first"
        )

    present = _column_names()

    for name, pg_type, not_null, default, backfill in COLUMNS:
        if name in present:
            continue

        if not not_null:
            op.execute(
                sa.text(f'ALTER TABLE "{TABLE}" ADD COLUMN IF NOT EXISTS "{name}" {pg_type}')
            )
            continue

        # NOT NULL without a server default would fail on a populated table.
        op.execute(
            sa.text(f'ALTER TABLE "{TABLE}" ADD COLUMN IF NOT EXISTS "{name}" {pg_type}')
        )
        literal = backfill or default
        if literal is not None:
            op.execute(
                sa.text(
                    f'UPDATE "{TABLE}" SET "{name}" = {literal} '
                    f'WHERE "{name}" IS NULL'
                )
            )
        op.execute(
            sa.text(f'ALTER TABLE "{TABLE}" ALTER COLUMN "{name}" SET NOT NULL')
        )
        if default is not None and default != backfill:
            op.execute(
                sa.text(
                    f'ALTER TABLE "{TABLE}" ALTER COLUMN "{name}" SET DEFAULT {default}'
                )
            )

    # Recreate indexes for any column that had to be re-added.
    present = _column_names()
    for name, columns, unique in INDEXES:
        if not set(columns) <= present:
            continue
        column_list = ", ".join(f'"{column}"' for column in columns)
        unique_sql = "UNIQUE " if unique else ""
        op.execute(
            sa.text(
                f'CREATE {unique_sql}INDEX IF NOT EXISTS "{name}" '
                f'ON "{TABLE}" ({column_list})'
            )
        )


def downgrade() -> None:
    """No-op: this repair only ever adds columns that the model requires.

    Dropping them would re-break the profile endpoints, so the columns are
    intentionally left in place.
    """
