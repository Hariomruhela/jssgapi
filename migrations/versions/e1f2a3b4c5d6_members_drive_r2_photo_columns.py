"""track the Google Drive / R2 photo migration on members

Revision ID: e1f2a3b4c5d6
Revises: d4e5f6a7b8c9
Create Date: 2026-09-30 00:00:00.000000

Member photos arrived from the Google Sheet as ``drive.google.com`` share
links. The mobile app renders them with a plain ``Image.network`` request, so
a link that is not shared "anyone with the link" resolves to an
``accounts.google.com`` sign-in page and the photo never loads.

The fix is to serve the bytes ourselves, which needs somewhere to record what
was migrated:

* ``profile_photo_drive_url`` / ``spouse_photo_drive_url`` keep the original
  Drive link, which is also what keeps a photo re-syncable from the sheet.
* ``profile_photo_r2_object_key`` / ``spouse_photo_r2_object_key`` are the
  deterministic R2 keys the bytes were copied to. Their presence is what makes
  the migration idempotent.
* ``profile_photo_r2_url`` / ``spouse_photo_r2_url`` are the URLs written to
  ``profile_photo_url`` and the ``member_photo_link`` / ``spouse_photo_link``
  entries of ``profile_data``.

Nothing is dropped, rewritten or backfilled: all six columns are nullable and
start empty, so the migration endpoint reports every photo as pending until it
runs. Existing rows keep whatever photo link they already have, including the
Drive links that do not render, so this revision cannot break the API on its
own.

Idempotent, so it is safe to run on a database that is already migrated.
"""

from __future__ import annotations

from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None

# Types match ``app/models/member.py`` exactly. ``schema_guard`` compares a
# compiled model type against the live column and only ever adds what is
# missing, so the two must agree or the guard and the migration would disagree
# about what a migrated database looks like.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("profile_photo_drive_url", "TEXT"),
    ("profile_photo_r2_object_key", "VARCHAR(500)"),
    ("profile_photo_r2_url", "TEXT"),
    ("spouse_photo_drive_url", "TEXT"),
    ("spouse_photo_r2_object_key", "VARCHAR(500)"),
    ("spouse_photo_r2_url", "TEXT"),
)


def upgrade() -> None:
    for column, column_type in COLUMNS:
        op.execute(
            f"ALTER TABLE members ADD COLUMN IF NOT EXISTS {column} {column_type}"
        )


def downgrade() -> None:
    for column, _column_type in COLUMNS:
        op.execute(f"ALTER TABLE members DROP COLUMN IF EXISTS {column}")