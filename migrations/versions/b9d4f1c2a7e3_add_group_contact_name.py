"""add a contact person name to social groups

Revision ID: b9d4f1c2a7e3
Revises: f4a5b6c7d8e9
Create Date: 2026-10-23 00:00:00.000000

The panel collects a group's contact person alongside its phone number. The
column is nullable so existing rows keep working; the startup schema guard can
also add it on deployments that do not run migrations.
"""

from __future__ import annotations

from alembic import op

revision = "b9d4f1c2a7e3"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE social_groups
            ADD COLUMN IF NOT EXISTS contact_name VARCHAR(255)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE social_groups DROP COLUMN IF EXISTS contact_name")
