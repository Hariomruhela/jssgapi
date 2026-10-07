"""Google Sheet header -> PostgreSQL column management for the sheet sync.

The registration sheet grows new columns over time. Instead of a migration per
column, the sync creates the columns it does not know about yet as ``TEXT`` on
``members`` - idempotently, and only ever as an ADD.

Rules honoured here:

* a column is only ever **added**, never dropped, renamed or retyped;
* the name is derived deterministically from the header, so the same header
  always lands in the same column (``"Member Occupation / Profession"`` ->
  ``member_occupation_profession``) and a re-run never creates a second one;
* headers that differ only by case/punctuation share one column, and the first
  non-empty cell wins;
* a column that already exists keeps its own type: the value is written only
  when the column already stores strings (and is truncated to its length), so
  a date/uuid/int column is never filled with free text;
* identity, audit and sync-managed columns (``id``, ``user_id``,
  ``contact_phone``, ...) are never written from a sheet cell.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

MEMBERS_TABLE = "members"

# A sheet must not be able to spray hundreds of columns into the schema in one
# run (a header typo would otherwise grow the table forever).
MAX_NEW_COLUMNS_PER_RUN = 100

# PostgreSQL identifiers are limited to 63 bytes.
MAX_IDENTIFIER_LENGTH = 63

# Only string columns are safe to receive an arbitrary sheet cell. Postgres
# reports ``character varying`` for VARCHAR and ``text`` for TEXT.
WRITABLE_DATA_TYPES = frozenset({"text", "character varying"})

# Columns the sync must never treat as sheet data: identity, audit and
# lifecycle columns, the photo/R2 pipeline, the JSONB profile bag, and the
# member columns the sync already writes from its mapped fields.
SYSTEM_COLUMNS = frozenset(
    {
        "id",
        "user_id",
        "group_id",
        "location_id",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "is_deleted",
        "deleted_at",
        "membership_number",
        "membership_status",
        "joined_at",
        "approved_at",
        "approved_by",
        "rejection_reason",
        "is_profile_complete",
        "profile_data",
        "first_name",
        "last_name",
        "date_of_birth",
        "contact_phone",
        "contact_email",
        "address_line",
        "occupation_summary",
        "profile_photo_url",
        "profile_photo_drive_url",
        "profile_photo_r2_object_key",
        "profile_photo_r2_url",
        "spouse_photo_drive_url",
        "spouse_photo_r2_object_key",
        "spouse_photo_r2_url",
    }
)

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class SheetColumn:
    """Where one sheet header's values are stored."""

    name: str
    # ``character_maximum_length`` of an existing VARCHAR(n); None for TEXT.
    max_length: int | None = None

    def fit(self, value: str) -> str:
        """Truncate so an existing VARCHAR(n) cannot reject the value."""
        if self.max_length and len(value) > self.max_length:
            return value[: self.max_length]
        return value


def normalize_column_name(header: str) -> str:
    """Turn a Google Sheet header into a safe PostgreSQL identifier.

    ``"Full Name"`` -> ``full_name``, ``"City / Area"`` -> ``city_area``,
    ``"Member Occupation"`` -> ``member_occupation``. Returns ``""`` when the
    header contains nothing usable (blank / symbols only / non-latin only).
    """
    name = re.sub(r"[^a-z0-9]+", "_", (header or "").strip().lower())
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        return ""
    if name[0].isdigit():
        name = f"col_{name}"
    if len(name) > MAX_IDENTIFIER_LENGTH:
        name = name[:MAX_IDENTIFIER_LENGTH].rstrip("_")
    return name if _IDENTIFIER_RE.match(name) else ""


async def plan_sheet_columns(
    session: AsyncSession,
    headers: list[str],
    keys: list[str | None],
    *,
    apply: bool,
) -> tuple[dict[str, SheetColumn], list[str]]:
    """Map every unmapped sheet header onto a writable ``members`` column.

    ``headers`` are the sheet's header cells and ``keys`` the registration field
    each one matched (``None`` = unknown to the sync). Returns
    ``(header -> column, new column names)``.

    With ``apply=False`` (dry run) nothing is written to PostgreSQL: the new
    column names are still reported so a dry run can preview them.
    """
    existing = {
        row["column_name"]: row
        for row in (
            await session.execute(
                text(
                    "SELECT column_name, data_type, character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = :table"
                ),
                {"table": MEMBERS_TABLE},
            )
        ).mappings()
    }
    if not existing:
        logger.warning(
            "sheet sync: table %r not found; headers left unmapped", MEMBERS_TABLE
        )
        return {}, []

    mapping: dict[str, SheetColumn] = {}
    new_columns: list[str] = []
    decided: dict[str, str] = {}  # column name -> first header that claimed it

    for header, key in zip(headers, keys, strict=True):
        if key is not None or not header.strip():
            continue
        name = normalize_column_name(header)
        if not name:
            logger.warning(
                "sheet sync: header %r has no usable column name; ignored", header
            )
            continue

        owner = decided.get(name)
        if owner is not None:
            # Two headers, one logical field: reuse the first decision so a
            # reworded header never creates a duplicate column.
            if owner != header:
                logger.warning(
                    "sheet sync: headers %r and %r both map to members.%s; "
                    "the first non-empty cell wins",
                    owner,
                    header,
                    name,
                )
            if owner in mapping:
                mapping[header] = mapping[owner]
            continue
        decided[name] = header

        info = existing.get(name)
        if info is None:
            if len(new_columns) >= MAX_NEW_COLUMNS_PER_RUN:
                logger.warning(
                    "sheet sync: refusing to add more than %s columns in one run; "
                    "header %r ignored",
                    MAX_NEW_COLUMNS_PER_RUN,
                    header,
                )
                continue
            if apply:
                try:
                    await session.execute(
                        text(
                            f"ALTER TABLE {MEMBERS_TABLE} "
                            f'ADD COLUMN IF NOT EXISTS "{name}" TEXT'
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one column must not abort
                    logger.error(
                        "sheet sync: could not add members.%s (%s): %s",
                        name,
                        type(exc).__name__,
                        exc,
                    )
                    continue
            new_columns.append(name)
            mapping[header] = SheetColumn(name)
            logger.info(
                "sheet sync: new column members.%s (TEXT)%s",
                name,
                "" if apply else " (dry run, not created)",
            )
            continue

        if name in SYSTEM_COLUMNS:
            logger.info(
                "sheet sync: header %r maps to managed column members.%s; "
                "the sync does not write it",
                header,
                name,
            )
            continue
        data_type = info["data_type"]
        if data_type not in WRITABLE_DATA_TYPES:
            logger.warning(
                "sheet sync: header %r maps to members.%s (%s), which does not "
                "store text; value not stored",
                header,
                name,
                data_type,
            )
            continue
        mapping[header] = SheetColumn(name, info["character_maximum_length"])
        logger.debug("sheet sync: header %r -> members.%s", header, name)

    return mapping, new_columns
