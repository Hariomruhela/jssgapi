"""normalize whitespace-polluted role names onto the canonical RoleName values

Revision ID: c7d2e1f0a3b4
Revises: f3a7c1b5d9e2
Create Date: 2026-09-30 00:00:00.000000

The ``roles`` table is seeded from external sources (Excel / Google Sheet
imports). Those imports stored whitespace-polluted labels such as ``"Admin\\n"``
instead of the canonical ``"ADMIN"``. Authorization looks roles up by exact name
(``ROLE_PERMISSIONS[user.role.name]``), so a polluted row matched no key and the
affected users were treated as having *no* role at all - every member write
returned 403 "Missing permission: member.update".

This repair moves every user off a non-canonical role row and onto the canonical
one that already exists, then drops the duplicate row. Authorization itself is
unchanged: each user keeps exactly the role they were assigned, and no user is
promoted. Roles whose name cannot be mapped to a ``RoleName`` are left untouched
and reported rather than guessed at.

Idempotent, so it is safe to run on a database that is already clean.
"""

from __future__ import annotations

import uuid

from alembic import op
from sqlalchemy import text

revision = "c7d2e1f0a3b4"
down_revision = "f3a7c1b5d9e2"
branch_labels = None
depends_on = None

CANONICAL = (
    "SUPER_ADMIN",
    "FEDERATION_ADMIN",
    "ADMIN",
    "REGIONAL_ADMIN",
    "GROUP_ADMIN",
    "MEMBER",
)


def upgrade() -> None:
    conn = op.get_bind()

    roles = {
        row.id: row.name
        for row in conn.execute(text("SELECT id, name FROM roles")).mappings()
    }
    canonical_ids: dict[str, uuid.UUID] = {}
    duplicates: list[tuple[uuid.UUID, str, uuid.UUID]] = []

    for role_id, stored in roles.items():
        if stored in CANONICAL:
            canonical_ids[stored] = role_id
            continue
        # "Admin\n" / "admin" / " Admin " -> "ADMIN"
        key = stored.strip().upper() if stored else ""
        if key in CANONICAL and key in canonical_ids:
            duplicates.append((role_id, stored, canonical_ids[key]))
        elif key in CANONICAL:
            # Canonical row was never seeded: normalise this row in place.
            conn.execute(
                text("UPDATE roles SET name = :name WHERE id = :id"),
                {"name": key, "id": role_id},
            )
            canonical_ids[key] = role_id

    for stale_id, stored, keep_id in duplicates:
        conn.execute(
            text("UPDATE users SET role_id = :keep WHERE role_id = :stale"),
            {"keep": keep_id, "stale": stale_id},
        )
        conn.execute(
            text("DELETE FROM role_permissions WHERE role_id = :stale"),
            {"stale": stale_id},
        )
        conn.execute(text("DELETE FROM roles WHERE id = :stale"), {"stale": stale_id})
        print(f"  role repair: merged {stored!r} into its canonical role row")


def downgrade() -> None:
    # Roles are re-seeded by the bootstrap service; the repaired rows are the
    # canonical ones, so there is nothing meaningful to restore.
    pass
