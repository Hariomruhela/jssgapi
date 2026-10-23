"""add state/city/contact persons and website to jain tirths

Revision ID: c3a7e9b2f801
Revises: b9d4f1c2a7e3
Create Date: 2026-10-23 16:40:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "c3a7e9b2f801"
down_revision = "b9d4f1c2a7e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jain_tirths ADD COLUMN IF NOT EXISTS state VARCHAR(100)")
    op.execute("ALTER TABLE jain_tirths ADD COLUMN IF NOT EXISTS city VARCHAR(100)")
    op.execute(
        "ALTER TABLE jain_tirths ADD COLUMN IF NOT EXISTS "
        "contact_person_name_1 VARCHAR(255)"
    )
    op.execute(
        "ALTER TABLE jain_tirths ADD COLUMN IF NOT EXISTS "
        "contact_person_phone_1 VARCHAR(20)"
    )
    op.execute(
        "ALTER TABLE jain_tirths ADD COLUMN IF NOT EXISTS "
        "contact_person_name_2 VARCHAR(255)"
    )
    op.execute(
        "ALTER TABLE jain_tirths ADD COLUMN IF NOT EXISTS "
        "contact_person_phone_2 VARCHAR(20)"
    )
    op.execute("ALTER TABLE jain_tirths ADD COLUMN IF NOT EXISTS website_url TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE jain_tirths DROP COLUMN IF EXISTS website_url")
    op.execute("ALTER TABLE jain_tirths DROP COLUMN IF EXISTS contact_person_phone_2")
    op.execute("ALTER TABLE jain_tirths DROP COLUMN IF EXISTS contact_person_name_2")
    op.execute("ALTER TABLE jain_tirths DROP COLUMN IF EXISTS contact_person_phone_1")
    op.execute("ALTER TABLE jain_tirths DROP COLUMN IF EXISTS contact_person_name_1")
    op.execute("ALTER TABLE jain_tirths DROP COLUMN IF EXISTS city")
    op.execute("ALTER TABLE jain_tirths DROP COLUMN IF EXISTS state")
