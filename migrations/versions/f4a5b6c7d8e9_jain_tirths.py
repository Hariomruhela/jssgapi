"""add the Jain Tirth directory and its image gallery

Revision ID: f4a5b6c7d8e9
Revises: d9e0f1a2b3c4
Create Date: 2026-10-23 00:00:00.000000

``jain_tirths`` is an event-like directory entry without a date or time. A
tirth carries a title, description, a single free-text location, a cover image
and a gallery of up to ten images. ``jain_tirth_images`` is the ordered link
between a tirth and the ``media`` row that tracks the stored object.

Idempotent throughout, so it is safe to run against a partially-migrated
database.
"""

from __future__ import annotations

from alembic import op

revision = "f4a5b6c7d8e9"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS jain_tirths (
            id UUID PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            description TEXT,
            location TEXT,
            cover_image_url TEXT,
            cover_image_object_key VARCHAR(500),
            status VARCHAR(20) NOT NULL DEFAULT 'PUBLISHED',
            is_public BOOLEAN NOT NULL DEFAULT TRUE,
            group_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by UUID,
            updated_by UUID,
            is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            deleted_at TIMESTAMPTZ,
            CONSTRAINT fk_jain_tirths_group_id_social_groups
                FOREIGN KEY (group_id) REFERENCES social_groups (id)
                ON DELETE SET NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_jain_tirths_group_id
            ON jain_tirths (group_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_jain_tirths_status
            ON jain_tirths (status)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_jain_tirths_is_deleted
            ON jain_tirths (is_deleted)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS jain_tirth_images (
            id UUID PRIMARY KEY,
            jain_tirth_id UUID NOT NULL,
            media_id UUID NOT NULL,
            url TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            uploaded_by UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by UUID,
            updated_by UUID,
            is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
            deleted_at TIMESTAMPTZ,
            CONSTRAINT uq_jain_tirth_images_media_id UNIQUE (media_id),
            CONSTRAINT fk_jain_tirth_images_jain_tirth_id_jain_tirths
                FOREIGN KEY (jain_tirth_id) REFERENCES jain_tirths (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_jain_tirth_images_media_id_media
                FOREIGN KEY (media_id) REFERENCES media (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_jain_tirth_images_uploaded_by_users
                FOREIGN KEY (uploaded_by) REFERENCES users (id)
                ON DELETE SET NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_jain_tirth_images_jain_tirth_id
            ON jain_tirth_images (jain_tirth_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_jain_tirth_images_is_deleted
            ON jain_tirth_images (is_deleted)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS jain_tirth_images")
    op.execute("DROP TABLE IF EXISTS jain_tirths")
