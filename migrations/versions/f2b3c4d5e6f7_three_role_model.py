"""collapse the admin hierarchy to SUPER_ADMIN / GROUP_ADMIN / MEMBER

Revision ID: f2b3c4d5e6f7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-05 00:00:00.000000

The platform had six roles. Three of them were never distinguishable in the
authorization code (``ADMIN`` and ``FEDERATION_ADMIN`` shared a hierarchy level,
and ``REGIONAL_ADMIN`` was scoped exactly like ``GROUP_ADMIN``), so they collapse:

* ``ADMIN`` -> ``SUPER_ADMIN`` (it already carried the full permission set)
* ``FEDERATION_ADMIN`` -> ``SUPER_ADMIN``
* ``REGIONAL_ADMIN`` -> ``GROUP_ADMIN`` (already group-scoped)

Users are repointed at the surviving role row *before* the retired rows are
deleted, so no account is left with a NULL ``role_id`` and nobody is locked out.
Their ``role_permissions`` grants are deleted along with the retired role, and the
surviving role's grants are re-seeded by ``bootstrap_service`` on the next app
start: a retired tier's stale grants must not outlive the tier that owned them.

``users.group_id`` is added and backfilled from ``members.group_id`` for existing
GROUP_ADMIN accounts. ``app/core/scope.py`` resolves a Group Admin's group from
this column, so without the backfill every currently-provisioned Group Admin
would be unable to administer anything until a SUPER_ADMIN re-assigned them.

Retired labels are matched after ``trim``/``upper`` because ``c7d2e1f0a3b4``
already normalised whitespace-polluted rows, but a database that skipped that
revision can still hold ``"Admin\\n"``. Those rows collapse too.

Nothing is dropped except the three retired ``roles`` rows, and only after every
user has been repointed. Every step is idempotent.
"""

from __future__ import annotations

import uuid

from alembic import op
from sqlalchemy import text

revision = "f2b3c4d5e6f7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None

SUPER_ADMIN = "SUPER_ADMIN"
GROUP_ADMIN = "GROUP_ADMIN"
MEMBER = "MEMBER"

# Retired label -> surviving label. Mirrors ``RETIRED_ROLE_ALIASES`` in
# ``app/core/constants.py``.
ROLE_COLLAPSE: dict[str, str] = {
    "ADMIN": SUPER_ADMIN,
    "FEDERATION_ADMIN": SUPER_ADMIN,
    "REGIONAL_ADMIN": GROUP_ADMIN,
}

# Matches the naming convention in ``app/models/base.py`` so ``schema_guard`` and
# this migration agree about what a migrated database looks like.
USERS_GROUP_FK = "fk_users_group_id_social_groups"


def _role_ids_by_label(conn) -> dict[str, list[uuid.UUID]]:
    """Every role row id per normalised label (a polluted duplicate may exist)."""
    rows = conn.execute(text("SELECT id, name FROM roles")).mappings()
    found: dict[str, list[uuid.UUID]] = {}
    for row in rows:
        key = (row["name"] or "").strip().upper()
        found.setdefault(key, []).append(row["id"])
    return found


def _ensure_role(conn, name: str) -> uuid.UUID:
    existing = conn.execute(
        text("SELECT id FROM roles WHERE name = :name"), {"name": name}
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    role_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO roles (id, name, description, created_at, updated_at) "
            "VALUES (:id, :name, :description, now(), now())"
        ),
        {"id": role_id, "name": name, "description": name},
    )
    return role_id


def upgrade() -> None:
    conn = op.get_bind()

    # 1. ``users.group_id``: the Group Admin's scope source of truth.
    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS group_id UUID"))

    fk_exists = conn.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
        {"name": USERS_GROUP_FK},
    ).scalar_one_or_none()
    if fk_exists is None:
        # A pre-existing constraint under a different name (from the schema guard,
        # or a hand-written deploy) has to go first, otherwise adding ours fails on
        # a duplicate foreign key.
        conn.execute(
            text(
                "ALTER TABLE users DROP CONSTRAINT IF EXISTS "
                "fk_users_group_id_users_group"
            )
        )
        conn.execute(
            text(
                f"ALTER TABLE users ADD CONSTRAINT {USERS_GROUP_FK} "
                "FOREIGN KEY (group_id) REFERENCES social_groups(id) "
                "ON DELETE SET NULL"
            )
        )
    conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_users_group_id ON users (group_id)")
    )

    role_ids = _role_ids_by_label(conn)
    surviving = {
        SUPER_ADMIN: _ensure_role(conn, SUPER_ADMIN),
        GROUP_ADMIN: _ensure_role(conn, GROUP_ADMIN),
        MEMBER: _ensure_role(conn, MEMBER),
    }

    # 2. Backfill from the member record, which is where a Group Admin's group
    #    used to live. Only for GROUP_ADMINs: nobody else should gain a group.
    conn.execute(
        text(
            """
            UPDATE users u
               SET group_id = m.group_id
              FROM (
                    SELECT DISTINCT ON (user_id) user_id, group_id
                      FROM members
                     WHERE user_id IS NOT NULL
                       AND group_id IS NOT NULL
                       AND is_deleted = false
                     ORDER BY user_id, updated_at DESC
                   ) m
             WHERE u.id = m.user_id
               AND u.group_id IS NULL
               AND u.role_id = :group_admin
            """
        ),
        {"group_admin": surviving[GROUP_ADMIN]},
    )

    # 3. Repoint every user on a retired role at its surviving role. Runs per
    #    retired label so the print output names what was collapsed.
    for retired, survivor in ROLE_COLLAPSE.items():
        stale_ids = role_ids.get(retired, [])
        for stale_id in stale_ids:
            moved = (
                conn.execute(
                    text(
                        "UPDATE users SET role_id = :survivor WHERE role_id = :stale "
                        "RETURNING id"
                    ),
                    {"survivor": surviving[survivor], "stale": stale_id},
                )
                .scalars()
                .all()
            )
            conn.execute(
                text("DELETE FROM role_permissions WHERE role_id = :stale"),
                {"stale": stale_id},
            )
            conn.execute(
                text("DELETE FROM roles WHERE id = :stale"), {"stale": stale_id}
            )
            print(
                f"  role collapse: moved {len(moved)} user(s) from {retired} "
                f"to {survivor}"
            )


def downgrade() -> None:
    # Recreating the retired rows cannot restore which accounts held them, so this
    # only reverts the schema change. Any Group Admin keeps the group it was
    # assigned, and ``Member.group_id`` still backs it up for scope resolution.
    conn = op.get_bind()
    conn.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS group_id"))
