"""correct the Group-ID city codes of Bhopal and Ujjain

Revision ID: d9e0f1a2b3c4
Revises: c8e3f4a5b6c7
Create Date: 2026-10-22 00:00:00.000000

The Group-ID city codes are *assigned*, never derived. The geo master data was
seeded by numbering each State's Urban Local Bodies in LGD order, which gave
the later cities of Madhya Pradesh three-digit codes: Bhopal became ``247``
and Ujjain ``161``, so their Group IDs read ``MP247003`` and ``MP161001``
instead of the required ``MP04001`` and ``MP13001``.

This revision applies the required mapping (``app.core.city_codes``):

===============  ========  ============  ==========
City             LGD code  old code      new code
===============  ========  ============  ==========
Churhat          248068    ``04``        ``414``
Sheopur          250673    ``13``        ``415``
Ujjain           250851    ``161``       ``13``
Bhopal           250946    ``247``       ``04``
===============  ========  ============  ==========

Churhat and Sheopur are re-homed to the next free codes in Madhya Pradesh
because ``04`` and ``13`` belong to Bhopal and Ujjain. No other city changes,
so no other prefix - and no other issued Group ID - moves.

Safety:

* every statement is guarded, so the revision is safe to re-run and safe
  against a database that already holds the corrected codes;
* the target codes must be free, otherwise the revision refuses to run
  instead of creating a duplicate prefix;
* a prefix that is given up must not be used by an issued Group ID; when it
  is, the revision refuses to run and leaves the data untouched;
* the two codes are applied through a temporary code, so the unique
  constraints on ``city_code``/``prefix`` are never violated half-way.

Group IDs that were already issued under an old code keep it: a Group ID is
never rewritten. The new series starts at ``001`` for the corrected code, so
the next group created in Bhopal is ``MP04001``.
"""

from alembic import op
from sqlalchemy import text

revision = "d9e0f1a2b3c4"
down_revision = "c8e3f4a5b6c7"
branch_labels = None
depends_on = None

#: (LGD city code, name, new city code, does the city give up its prefix).
#: A city that gives up its prefix must not be referenced by an issued Group
#: ID; a city that takes one over may be - its old IDs stay as they are.
CORRECTIONS = (
    (248068, "Churhat", "414", True),
    (250673, "Sheopur", "415", True),
    (250851, "Ujjain", "13", False),
    (250946, "Bhopal", "04", False),
)

#: The codes the revision falls back to when it is rolled back.
ORIGINAL_CODES = {
    248068: "04",
    250673: "13",
    250851: "161",
    250946: "247",
}


def _city_rows() -> list[dict]:
    """

from __future__ import annotations

The cities the revision touches, with their current code and prefix."""
    bind = op.get_bind()
    rows = []
    for lgd_code, name, new_code, releases in CORRECTIONS:
        row = bind.execute(
            text(
                "SELECT c.id AS id, c.city_code AS city_code, c.prefix AS prefix, "
                "s.code AS state_code "
                "FROM geo_cities c "
                "JOIN geo_states s ON s.id = c.state_id "
                "WHERE c.lgd_code = :lgd"
            ),
            {"lgd": lgd_code},
        ).fetchone()
        if row is None:
            # The city is not part of this database's master data - nothing to
            # correct.
            continue
        rows.append(
            {
                "id": row.id,
                "lgd_code": lgd_code,
                "name": name,
                "city_code": row.city_code,
                "prefix": row.prefix,
                "state_code": row.state_code,
                "new_code": new_code,
                "releases": releases,
            }
        )
    return rows


def _assert_safe(rows: list[dict], codes: dict[int, str]) -> None:
    """Refuse to run when a correction would collide or orphan an issued ID."""
    bind = op.get_bind()
    for row in rows:
        target_code = codes[row["lgd_code"]]
        if target_code == row["city_code"]:
            continue
        target_prefix = f"{row['state_code']}{target_code}"
        taken = bind.execute(
            text(
                "SELECT 1 FROM geo_cities "
                "WHERE prefix = :prefix AND id <> :id LIMIT 1"
            ),
            {"prefix": target_prefix, "id": row["id"]},
        ).fetchone()
        if taken is not None:
            raise RuntimeError(
                f"Cannot apply the Group-ID city code {target_code!r} to "
                f"{row['name']!r}: the prefix {target_prefix!r} is already "
                "used by another city. Resolve the collision before running "
                "this migration."
            )
        if row["releases"]:
            used = bind.execute(
                text(
                    "SELECT group_id FROM social_groups "
                    "WHERE group_id LIKE :prefix || '%' LIMIT 1"
                ),
                {"prefix": row["prefix"]},
            ).fetchone()
            if used is not None:
                raise RuntimeError(
                    f"Cannot move {row['name']!r} off the prefix "
                    f"{row['prefix']!r}: group {used.group_id!r} already uses "
                    "it. Rewrite or reassign that Group ID before running this "
                    "migration."
                )


def _apply(rows: list[dict], codes: dict[int, str]) -> None:
    """Write the new codes, through a temporary code so the unique
    constraints on ``city_code``/``prefix`` are never violated half-way."""
    bind = op.get_bind()
    pending = [r for r in rows if codes[r["lgd_code"]] != r["city_code"]]
    for row in pending:
        temporary = f"T{row['lgd_code']}"
        bind.execute(
            text(
                "UPDATE geo_cities SET city_code = :code, prefix = :prefix "
                "WHERE id = :id"
            ),
            {
                "code": temporary,
                "prefix": f"{row['state_code']}{temporary}",
                "id": row["id"],
            },
        )
    for row in pending:
        target_code = codes[row["lgd_code"]]
        bind.execute(
            text(
                "UPDATE geo_cities SET city_code = :code, prefix = :prefix "
                "WHERE id = :id"
            ),
            {
                "code": target_code,
                "prefix": f"{row['state_code']}{target_code}",
                "id": row["id"],
            },
        )


"""correct the Group-ID city codes of Bhopal and Ujjain

Revision ID: d9e0f1a2b3c4
Revises: c8e3f4a5b6c7
Create Date: 2026-10-22 00:00:00.000000

The Group-ID city codes are *assigned*, never derived. The geo master data was
seeded by numbering each State's Urban Local Bodies in LGD order, which gave
the later cities of Madhya Pradesh three-digit codes: Bhopal became ``247``
and Ujjain ``161``, so their Group IDs read ``MP247003`` and ``MP161001``
instead of the required ``MP04001`` and ``MP13001``.

This revision applies the required mapping (``app.core.city_codes``):

===============  ========  ============  ==========
City             LGD code  old code      new code
===============  ========  ============  ==========
Churhat          248068    ``04``        ``414``
Sheopur          250673    ``13``        ``415``
Ujjain           250851    ``161``       ``13``
Bhopal           250946    ``247``       ``04``
===============  ========  ============  ==========

Churhat and Sheopur are re-homed to the next free codes in Madhya Pradesh
because ``04`` and ``13`` belong to Bhopal and Ujjain. No other city changes,
so no other prefix - and no other issued Group ID - moves.

Safety:

* every statement is guarded, so the revision is safe to re-run and safe
  against a database that already holds the corrected codes;
* the target codes must be free, otherwise the revision refuses to run
  instead of creating a duplicate prefix;
* a prefix that is given up must not be used by an issued Group ID; when it
  is, the revision refuses to run and leaves the data untouched;
* the codes are applied through a temporary code, so the unique
  constraints on ``city_code``/``prefix`` are never violated half-way.
"""

from alembic import op
from sqlalchemy import text

revision = "d9e0f1a2b3c4"
down_revision = "c8e3f4a5b6c7"
branch_labels = None
depends_on = None

#: (LGD city code, name, new city code, does the city give up its prefix).
#: A city that gives up its prefix must not be referenced by an issued Group
#: ID; a city that takes one over may be - its old IDs stay as they are.
CORRECTIONS = (
    (248068, "Churhat", "414", True),
    (250673, "Sheopur", "415", True),
    (250851, "Ujjain", "13", False),
    (250946, "Bhopal", "04", False),
)

#: The codes the revision falls back to when it is rolled back.
ORIGINAL_CODES = {
    248068: "04",
    250673: "13",
    250851: "161",
    250946: "247",
}


def _city_rows() -> list[dict]:
    """The cities the revision touches, with their current code and prefix."""
    bind = op.get_bind()
    rows = []
    for lgd_code, name, new_code, releases in CORRECTIONS:
        row = bind.execute(
            text(
                "SELECT c.id AS id, c.city_code AS city_code, c.prefix AS prefix, "
                "s.code AS state_code "
                "FROM geo_cities c "
                "JOIN geo_states s ON s.id = c.state_id "
                "WHERE c.lgd_code = :lgd"
            ),
            {"lgd": lgd_code},
        ).fetchone()
        if row is None:
            continue
        rows.append(
            {
                "id": row.id,
                "lgd_code": lgd_code,
                "name": name,
                "city_code": row.city_code,
                "prefix": row.prefix,
                "state_code": row.state_code,
                "new_code": new_code,
                "releases": releases,
            }
        )
    return rows


def _assert_safe(rows: list[dict], codes: dict[int, str]) -> None:
    bind = op.get_bind()
    # Cities that are planned to move away from their current prefix
    moving_away = {r["lgd_code"] for r in rows if codes[r["lgd_code"]] != r["city_code"]}
    for row in rows:
        target_code = codes[row["lgd_code"]]
        if target_code == row["city_code"]:
            continue
        target_prefix = f"{row['state_code']}{target_code}"
        taken = bind.execute(
            text(
                "SELECT 1 FROM geo_cities "
                "WHERE prefix = :prefix AND id <> :id LIMIT 1"
            ),
            {"prefix": target_prefix, "id": row["id"]},
        ).fetchone()
        if taken is not None:
            # If the city holding the target prefix is also being moved in this batch, it's fine
            taken_row = bind.execute(
                text("SELECT lgd_code FROM geo_cities WHERE prefix = :prefix AND id <> :id LIMIT 1"),
                {"prefix": target_prefix, "id": row["id"]},
            ).fetchone()
            if taken_row and taken_row.lgd_code in moving_away:
                continue  # will be moved
            raise RuntimeError(
                f"Cannot apply the Group-ID city code {target_code!r} to "
                f"{row['name']!r}: the prefix {target_prefix!r} is already "
                "used by another city. Resolve the collision before running "
                "this migration."
            )
        if row["releases"]:
            used = bind.execute(
                text(
                    "SELECT group_id FROM social_groups "
                    "WHERE group_id LIKE :prefix || '%' LIMIT 1"
                ),
                {"prefix": row["prefix"]},
            ).fetchone()
            if used is not None:
                raise RuntimeError(
                    f"Cannot move {row['name']!r} off the prefix "
                    f"{row['prefix']!r}: group {used.group_id!r} already uses "
                    "it. Rewrite or reassign that Group ID before running this "
                    "migration."
                )


def _apply(rows: list[dict], codes: dict[int, str]) -> None:
    bind = op.get_bind()
    pending = [r for r in rows if codes[r["lgd_code"]] != r["city_code"]]
    for row in pending:
        temporary = f"T{row['lgd_code']}"
        bind.execute(
            text(
                "UPDATE geo_cities SET city_code = :code, prefix = :prefix "
                "WHERE id = :id"
            ),
            {
                "code": temporary,
                "prefix": f"{row['state_code']}{temporary}",
                "id": row["id"],
            },
        )
    for row in pending:
        target_code = codes[row["lgd_code"]]
        bind.execute(
            text(
                "UPDATE geo_cities SET city_code = :code, prefix = :prefix "
                "WHERE id = :id"
            ),
            {
                "code": target_code,
                "prefix": f"{row['state_code']}{target_code}",
                "id": row["id"],
            },
        )


def upgrade() -> None:
    rows = _city_rows()
    if not rows:
        return
    codes = {lgd: new_code for lgd, _name, new_code, _releases in CORRECTIONS}
    # Move cities that give up the codes first, so the required cities can take them.
    rows_by_lgd = {r["lgd_code"]: r for r in rows}
    order = [248068, 250673, 250851, 250946]
    ordered = [rows_by_lgd[l] for l in order if l in rows_by_lgd]
    _assert_safe(ordered, codes)
    _apply(ordered, codes)


def downgrade() -> None:
    rows = _city_rows()
    if not rows:
        return
    rows_by_lgd = {r["lgd_code"]: r for r in rows}
    order = [250851, 250946, 248068, 250673]
    ordered = [rows_by_lgd[l] for l in order if l in rows_by_lgd]
    _assert_safe(ordered, ORIGINAL_CODES)
    _apply(ordered, ORIGINAL_CODES)
