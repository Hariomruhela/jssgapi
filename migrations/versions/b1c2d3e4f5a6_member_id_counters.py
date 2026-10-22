"""member IDs: per-group sequence table for members.membership_number

Revision ID: b1c2d3e4f5a6
Revises: e7f8a9b0c1d2
Create Date: 2026-10-21 00:00:00.000000

Adding a member to a group now mints a member ID derived from the owning
group's ID: ``MP09001`` + ``001`` -> ``MP09001001``, ``MP09001002``, ... The
per-group counter lives here in ``member_id_counters`` (one row per Group ID)
and is advanced by the same atomic ``INSERT ... ON CONFLICT DO UPDATE ...
RETURNING`` upsert used for ``group_id_counters``, so concurrent adds in one
group can never receive the same member ID and the sequence never resets.

``members.membership_number`` already exists (unique, nullable) and is the
storage column - it is NOT altered here. Existing rows (sheet imports,
directory loads) are left untouched; a number is only written when a member
enters a group through the API.

Only a new table is created - nothing existing is touched. Every statement is
guarded (``IF NOT EXISTS``), so the revision is safe to re-run and safe against
a partially-migrated database. Downgrade drops the table and with it every
member sequence - export it first if the values matter.
"""

from __future__ import annotations

from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS member_id_counters (
            group_id VARCHAR(16) PRIMARY KEY,
            last_value INTEGER NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS member_id_counters")
