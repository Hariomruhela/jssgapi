"""add member profile_data

Revision ID: c4d8e9f0a1b2
Revises: b7c2f9a3e1d0
Create Date: 2026-09-25 00:00:00.000000

``members.profile_data`` stores the profile API fields that have no dedicated
column (spouse_*, son_*, daughter_*, *_education, anniversary_date,
interest_fields, ...). It is read and written by ProfileService, so the column
is required for ``GET/PUT /api/v1/profile`` to work at all.

The DDL is idempotent: environments where the column already exists (or where
this revision was stamped without the DDL actually running) converge instead of
failing with DuplicateColumn.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4d8e9f0a1b2"
down_revision = "b7c2f9a3e1d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE members "
            "ADD COLUMN IF NOT EXISTS profile_data JSONB "
            "DEFAULT '{}'::jsonb NOT NULL"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE members DROP COLUMN IF EXISTS profile_data"))
