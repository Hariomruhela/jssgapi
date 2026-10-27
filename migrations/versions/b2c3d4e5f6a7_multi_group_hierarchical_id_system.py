"""implement resilient multi-group hierarchical id generation system

Revision ID: b2c3d4e5f6a7
Revises: a8b7c6d5e4f3
Create Date: 2026-10-27 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b2c3d4e5f6a7"
down_revision = "a8b7c6d5e4f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Counter table for Groups per District
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS district_group_counters (
            state_code VARCHAR(2) NOT NULL,
            district_code VARCHAR(2) NOT NULL,
            last_group_seq INT NOT NULL DEFAULT 0,
            PRIMARY KEY (state_code, district_code)
        )
        """
    )

    # 2. Counter table for Members per Group
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS group_member_counters (
            group_uuid UUID NOT NULL PRIMARY KEY REFERENCES social_groups(id) ON DELETE CASCADE,
            last_member_seq INT NOT NULL DEFAULT 0
        )
        """
    )

    # 3. Create view 'groups' alias for social_groups
    op.execute(
        """
        CREATE OR REPLACE VIEW groups AS SELECT * FROM social_groups
        """
    )

    # 4. Many-to-Many Junction Table for Multi-Group Membership
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS group_members (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            group_id UUID NOT NULL REFERENCES social_groups(id) ON DELETE CASCADE,
            membership_number VARCHAR(30) NOT NULL,
            is_primary BOOLEAN NOT NULL DEFAULT FALSE,
            role VARCHAR(50) NOT NULL DEFAULT 'MEMBER',
            status VARCHAR(20) NOT NULL DEFAULT 'APPROVED',
            joined_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            created_by UUID,
            updated_by UUID,
            CONSTRAINT uq_user_group UNIQUE (user_id, group_id),
            CONSTRAINT uq_group_membership_number UNIQUE (group_id, membership_number)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_group_members_primary ON group_members(user_id, is_primary)")

    # 5. Add membership_number to member_group_memberships
    op.execute(
        """
        ALTER TABLE member_group_memberships ADD COLUMN IF NOT EXISTS membership_number VARCHAR(30)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS group_members CASCADE")
    op.execute("DROP VIEW IF EXISTS groups CASCADE")
    op.execute("DROP TABLE IF EXISTS group_member_counters CASCADE")
    op.execute("DROP TABLE IF EXISTS district_group_counters CASCADE")
    op.execute("ALTER TABLE member_group_memberships DROP COLUMN IF EXISTS membership_number")
