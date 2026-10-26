"""add member group memberships and backfill primary groups

Revision ID: a8b7c6d5e4f3
Revises: c3a7e9b2f801
Create Date: 2026-10-26 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a8b7c6d5e4f3"
down_revision = "c3a7e9b2f801"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "member_group_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "priority >= 1", name="ck_member_group_memberships_priority_positive"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["social_groups.id"],
            name="fk_member_group_memberships_group_id_social_groups",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["members.id"],
            name="fk_member_group_memberships_member_id_members",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_member_group_memberships"),
        sa.UniqueConstraint(
            "member_id",
            "group_id",
            name="uq_member_group_memberships_member_group",
        ),
        sa.UniqueConstraint(
            "member_id",
            "priority",
            name="uq_member_group_memberships_member_priority",
        ),
    )
    op.create_index(
        "ix_member_group_memberships_group_id",
        "member_group_memberships",
        ["group_id"],
    )
    op.create_index(
        "ix_member_group_memberships_member_id",
        "member_group_memberships",
        ["member_id"],
    )

    op.execute(
        """
        INSERT INTO member_group_memberships (
            id, member_id, group_id, priority, joined_at
        )
        SELECT gen_random_uuid(), m.id, m.group_id, 1, m.joined_at
        FROM members AS m
        WHERE m.group_id IS NOT NULL
        ON CONFLICT (member_id, group_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_member_group_memberships_member_id",
        table_name="member_group_memberships",
    )
    op.drop_index(
        "ix_member_group_memberships_group_id",
        table_name="member_group_memberships",
    )
    op.drop_table("member_group_memberships")
