"""add firebase_uid to users

Revision ID: b7c2f9a3e1d0
Revises: 9b91379e04cc
Create Date: 2026-09-23 00:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "b7c2f9a3e1d0"
down_revision = "9b91379e04cc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("firebase_uid", sa.String(length=128), nullable=True)
        )
        batch_op.create_index(
            op.f("ix_users_firebase_uid"), ["firebase_uid"]
        )
        batch_op.create_unique_constraint(
            "uq_users_firebase_uid", ["firebase_uid"]
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("uq_users_firebase_uid", type_="unique")
        batch_op.drop_index(op.f("ix_users_firebase_uid"))
        batch_op.drop_column("firebase_uid")