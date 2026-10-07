"""Distinct group names as they appear on the registration Google Sheet.

The ``social_groups`` table only holds groups that were actually created; the
sheet carries free-text group names per row. ``GET /api/v1/groups?source=sheets``
reads them so a picker can show what the sheet says without a database write.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import get_settings
from app.core.exceptions import ServiceUnavailableException
from app.services.google_sheets_service import GoogleSheetsService
from app.services.registration_sync_service import (
    build_column_index,
    is_placeholder,
    match_field,
)

#: Sheet columns that hold a group name. ``social_group_name`` is the social
#: group/association a member belongs to; ``group_designation`` is their post
#: in it.
GROUP_FIELDS = ("social_group_name", "group_designation")


def _slugify(name: str) -> str:
    """A stable, URL-safe id for a group name that has no row id."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:80].strip("-") or "group"


def read_sheet_groups() -> list[dict[str, Any]]:
    """Every distinct, non-placeholder group value on the sheet.

    Returns ``{"id", "name", "field"}`` items sorted by name. Two values that
    only differ in case are one group; the same name under both columns stays
    twice, because the two columns mean different things.
    """
    settings = get_settings()
    spreadsheet_id = (settings.google_sheets_spreadsheet_id or "").strip()
    if not spreadsheet_id:
        raise ServiceUnavailableException(
            "GOOGLE_SHEETS_SPREADSHEET_ID is not configured",
            "SHEETS_NOT_CONFIGURED",
        )
    table = GoogleSheetsService().read_table(
        spreadsheet_id,
        settings.google_sheets_sheet_name,
        settings.google_sheets_header_row,
        settings.google_sheets_data_start_row,
    )

    index = build_column_index()
    columns: dict[str, list[int]] = {field: [] for field in GROUP_FIELDS}
    for position, (_letter, header) in enumerate(table.columns):
        field = match_field(index, header)
        if field in columns:
            columns[field].append(position)

    names: dict[str, list[str]] = {field: [] for field in GROUP_FIELDS}
    seen: dict[str, set[str]] = {field: set() for field in GROUP_FIELDS}
    for _row_number, cells in table.rows:
        for field in GROUP_FIELDS:
            for position in columns[field]:
                text = cells[position].strip() if position < len(cells) else ""
                if not text or is_placeholder(text):
                    continue
                key = text.casefold()
                if key in seen[field]:
                    continue
                seen[field].add(key)
                names[field].append(text)

    items: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for field in GROUP_FIELDS:
        for name in sorted(names[field], key=str.casefold):
            slug = _slugify(name)
            unique = slug
            counter = 2
            while unique in used_ids:
                unique = f"{slug}-{counter}"
                counter += 1
            used_ids.add(unique)
            items.append({"id": unique, "name": name, "field": field})
    return items
