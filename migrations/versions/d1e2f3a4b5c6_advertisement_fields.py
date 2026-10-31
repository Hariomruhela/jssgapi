"""add advertisement fields for person details, images, amount and subscription

Revision ID: d1e2f3a4b5c6
Revises: b2c3d4e5f6a7
Create Date: 2026-10-31 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE advertisements
            ADD COLUMN IF NOT EXISTS person_name VARCHAR(255),
            ADD COLUMN IF NOT EXISTS mobile_number VARCHAR(20),
            ADD COLUMN IF NOT EXISTS cover_image_url TEXT,
            ADD COLUMN IF NOT EXISTS cover_image_object_key VARCHAR(500),
            ADD COLUMN IF NOT EXISTS large_image_url TEXT,
            ADD COLUMN IF NOT EXISTS large_image_object_key VARCHAR(500),
            ADD COLUMN IF NOT EXISTS amount NUMERIC(10, 2) DEFAULT 0,
            ADD COLUMN IF NOT EXISTS subscription_months INTEGER DEFAULT 1,
            ADD COLUMN IF NOT EXISTS payment_status VARCHAR(20) DEFAULT 'PENDING',
            ADD COLUMN IF NOT EXISTS payment_id UUID;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_advertisements_payment_id_payments'
            ) THEN
                ALTER TABLE advertisements
                    ADD CONSTRAINT fk_advertisements_payment_id_payments
                    FOREIGN KEY (payment_id) REFERENCES payments (id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE advertisements
            DROP CONSTRAINT IF EXISTS fk_advertisements_payment_id_payments;
        """
    )
    op.execute(
        """
        ALTER TABLE advertisements
            DROP COLUMN IF EXISTS payment_id,
            DROP COLUMN IF EXISTS payment_status,
            DROP COLUMN IF EXISTS subscription_months,
            DROP COLUMN IF EXISTS amount,
            DROP COLUMN IF EXISTS large_image_object_key,
            DROP COLUMN IF EXISTS large_image_url,
            DROP COLUMN IF EXISTS cover_image_object_key,
            DROP COLUMN IF EXISTS cover_image_url,
            DROP COLUMN IF EXISTS mobile_number,
            DROP COLUMN IF EXISTS person_name;
        """
    )
