"""event host, type, wall-clock timing and the per-event image gallery

Revision ID: c8e3f4a5b6c7
Revises: b1c2d3e4f5a6
Create Date: 2026-10-21 00:00:00.000000

An event gains the details a host actually fills in when they publish one:

* ``event_date`` / ``event_time`` - the date and time as a person writes
  them, in the platform timezone. ``starts_at`` stays the instant and is
  derived from the pair, so there is still exactly one moment to sort and
  compare on.
* ``event_type`` / ``event_type_other`` - the kind of programme, chosen from
  a fixed list with a free-text escape hatch.
* ``host_id`` / ``host_name`` / ``host_contact_number`` - who is running the
  event. It defaults to the account that created it.
* ``cover_image_object_key`` - the storage key of the cover image, so a
  replacement can delete the object it replaces.

``event_images`` is a new table: the ordered link between an event and the
``media`` row that tracks a stored object. An event holds at most ten of
them; the limit is enforced by the API, not the database.

Existing rows are backfilled from ``starts_at`` (converted into the platform
timezone), so no event is left without a date and a time. Idempotent
throughout, so it is safe to run against a partially-migrated database.
"""

from __future__ import annotations

from alembic import op

revision = "c8e3f4a5b6c7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None

#: The timezone the wall-clock date and time are expressed in.
APP_TIMEZONE = "Asia/Kolkata"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE events
            ADD COLUMN IF NOT EXISTS event_date DATE,
            ADD COLUMN IF NOT EXISTS event_time TIME WITHOUT TIME ZONE,
            ADD COLUMN IF NOT EXISTS event_type VARCHAR(40),
            ADD COLUMN IF NOT EXISTS event_type_other VARCHAR(255),
            ADD COLUMN IF NOT EXISTS host_id UUID,
            ADD COLUMN IF NOT EXISTS host_name VARCHAR(255),
            ADD COLUMN IF NOT EXISTS host_contact_number VARCHAR(20),
            ADD COLUMN IF NOT EXISTS cover_image_object_key VARCHAR(500)
        """
    )

    # Existing events only have an instant. Read the wall-clock date and time
    # back out of it so every row has both.
    op.execute(
        f"""
        UPDATE events
        SET event_date = (starts_at AT TIME ZONE '{APP_TIMEZONE}')::date,
            event_time = (starts_at AT TIME ZONE '{APP_TIMEZONE}')::time
        WHERE event_date IS NULL OR event_time IS NULL
        """
    )

    op.execute(
        """
        UPDATE events SET event_type = 'OTHER' WHERE event_type IS NULL
        """
    )

    op.execute(
        """
        ALTER TABLE events
            ALTER COLUMN event_date SET DEFAULT CURRENT_DATE,
            ALTER COLUMN event_date SET NOT NULL,
            ALTER COLUMN event_time SET DEFAULT CURRENT_TIME,
            ALTER COLUMN event_time SET NOT NULL,
            ALTER COLUMN event_type SET DEFAULT 'OTHER',
            ALTER COLUMN event_type SET NOT NULL
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_events_event_date ON events (event_date)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_events_event_type ON events (event_type)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_events_host_id ON events (host_id)
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_events_host_id_users'
            ) THEN
                ALTER TABLE events
                    ADD CONSTRAINT fk_events_host_id_users
                    FOREIGN KEY (host_id) REFERENCES users (id) ON DELETE SET NULL;
            END IF;
        END $$
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS event_images (
            id UUID PRIMARY KEY,
            event_id UUID NOT NULL,
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
            CONSTRAINT uq_event_images_media_id UNIQUE (media_id),
            CONSTRAINT fk_event_images_event_id_events
                FOREIGN KEY (event_id) REFERENCES events (id) ON DELETE CASCADE,
            CONSTRAINT fk_event_images_media_id_media
                FOREIGN KEY (media_id) REFERENCES media (id) ON DELETE CASCADE,
            CONSTRAINT fk_event_images_uploaded_by_users
                FOREIGN KEY (uploaded_by) REFERENCES users (id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_event_images_event_id
            ON event_images (event_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_event_images_is_deleted
            ON event_images (is_deleted)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS event_images")
    op.execute(
        """
        ALTER TABLE events DROP CONSTRAINT IF EXISTS fk_events_host_id_users
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_events_event_date")
    op.execute("DROP INDEX IF EXISTS ix_events_event_type")
    op.execute("DROP INDEX IF EXISTS ix_events_host_id")
    op.execute(
        """
        ALTER TABLE events
            DROP COLUMN IF EXISTS cover_image_object_key,
            DROP COLUMN IF EXISTS host_contact_number,
            DROP COLUMN IF EXISTS host_name,
            DROP COLUMN IF EXISTS host_id,
            DROP COLUMN IF EXISTS event_type_other,
            DROP COLUMN IF EXISTS event_type,
            DROP COLUMN IF EXISTS event_time,
            DROP COLUMN IF EXISTS event_date
        """
    )
