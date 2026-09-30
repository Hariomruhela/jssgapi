"""make members.user_id optional so community members can exist without a login

Revision ID: d4e5f6a7b8c9
Revises: c7d2e1f0a3b4
Create Date: 2026-09-30 00:00:00.000000

A ``Member`` is a community profile; a ``User`` is a login account. They are
distinct concepts, but ``members.user_id`` was ``NOT NULL`` with
``ON DELETE CASCADE``, which forced two things:

* every community member (including Google Sheet imports) to have a login
  account, so an Admin could not create or edit a member without first
  provisioning a User - the panel refused with "The API requires an existing
  user for every member record.";
* deleting a User silently destroyed the member profile and all of its family,
  professional and profile data.

``user_id`` becomes nullable and the foreign key becomes ``ON DELETE SET NULL``,
so removing a login no longer deletes community data and an Admin can manage a
member that has no account.

Nothing is dropped or rewritten: the column, the unique constraint and every
existing row are preserved. Only the NOT NULL constraint is relaxed and the FK
action is changed. Postgres permits many NULLs under a UNIQUE constraint, so
``uq_members_user_id`` still enforces at most one member per user account.

Idempotent, so it is safe to run on a database that is already migrated.
"""

from __future__ import annotations

from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c7d2e1f0a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE members ALTER COLUMN user_id DROP NOT NULL")
    # Preserve the member profile when a login account is removed.
    op.execute(
        """
        DO $$
        DECLARE constraint_name text;
        BEGIN
            SELECT conname INTO constraint_name
            FROM pg_constraint
            WHERE conrelid = 'members'::regclass
              AND contype = 'f'
              AND conkey = ARRAY[(SELECT attnum FROM pg_attribute
                                  WHERE attrelid = 'members'::regclass
                                    AND attname = 'user_id')];

            IF constraint_name IS NOT NULL THEN
                EXECUTE format(
                    'ALTER TABLE members DROP CONSTRAINT %I', constraint_name);
            END IF;
        END $$;
        """
    )
    op.execute(
        "ALTER TABLE members ADD CONSTRAINT fk_members_user_id "
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    # Reinstating NOT NULL is only possible once every user-less member has been
    # linked to an account. Refuse loudly rather than deleting community records.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM members WHERE user_id IS NULL) THEN
                RAISE EXCEPTION
                    'cannot restore NOT NULL: % member(s) have no linked user. '
                    'Link them to an account first.',
                    (SELECT count(*) FROM members WHERE user_id IS NULL);
            END IF;
        END $$;
        """
    )
    op.execute(
        "ALTER TABLE members DROP CONSTRAINT IF EXISTS fk_members_user_id"
    )
    op.execute(
        "ALTER TABLE members ADD CONSTRAINT fk_members_user_id "
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
    )
    op.execute("ALTER TABLE members ALTER COLUMN user_id SET NOT NULL")
