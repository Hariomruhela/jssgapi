"""add media_objects table for the Postgres media backend

Revision ID: f3a7c1b5d9e2
Revises: e6f1a2b3c4d5
Create Date: 2026-09-29 00:00:00.000000

Cloudflare R2 is not implemented yet, so uploads fell back to the local disk.
On Vercel that disk belongs to a single serverless instance and is discarded on
a cold start, which meant a successful upload response could point at bytes that
no longer existed.

This adds the ``media_objects`` table so ``DatabaseStorage`` can keep the bytes
in Postgres, which is already configured and shared across every instance. The
API already streams bytes back from ``/api/v1/media/{media_id}/content``, so no
public URL or CDN is needed.

Idempotent like the rest of the chain, so it is safe on a database that already
has the table.
"""

from __future__ import annotations

from alembic import op

revision = "f3a7c1b5d9e2"
down_revision = "e6f1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS media_objects (
            object_key  VARCHAR(500) PRIMARY KEY,
            content     BYTEA       NOT NULL,
            mime_type   VARCHAR(100) NOT NULL,
            file_size   BIGINT      NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS media_objects")
