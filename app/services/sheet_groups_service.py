"""Group names from the registration Google Sheet and the member directory.

The ``social_groups`` table is the source of truth for the Groups CRUD API.
Group names, however, arrive as free text - on the sheet's
``social_group_name`` column and on each member's ``profile_data`` - so they
had no row to read, edit or delete. This module reads the distinct names from
both sources and materializes the missing ones into ``social_groups``
(:func:`import_member_groups`), then links matching members to their group so
the group's member list, scope and delete all work on real rows.

It also still answers ``GET /api/v1/groups?source=sheets`` - the distinct,
non-persisted names as the sheet spells them - for a picker that wants the raw
sheet values.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import ServiceUnavailableException
from app.models.group import SocialGroup
from app.models.member import Member
from app.repositories.group_repository import GroupRepository
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

#: ``social_groups.name`` is VARCHAR(255); a sheet cell longer than that is
#: truncated rather than rejected.
NAME_MAX = 255

#: Free-text cells that are not group names: a yes/no answer and a blood group
#: (the form's group column is occasionally filled with one).
_NON_GROUP_VALUES = frozenset({"no", "yes"})
_BLOOD_GROUP = re.compile(r"(?:a|b|ab|o)\s*[+\-−]", re.IGNORECASE)


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


def sheet_social_group_names() -> list[str]:
    """Distinct ``social_group_name`` values on the sheet (blocking network I/O)."""
    return [
        item["name"]
        for item in read_sheet_groups()
        if item["field"] == "social_group_name"
    ]


@dataclass
class GroupImportSummary:
    """What :func:`import_member_groups` created, kept and linked."""

    sheet_names_read: int = 0
    member_names_read: int = 0
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    members_linked: int = 0
    total_groups: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_names_read": self.sheet_names_read,
            "member_names_read": self.member_names_read,
            "created": self.created,
            "existing": self.existing,
            "skipped": self.skipped,
            "members_linked": self.members_linked,
            "total_groups": self.total_groups,
        }

    @property
    def message(self) -> str:
        return (
            f"Group import complete: {len(self.created)} created, "
            f"{len(self.existing)} already existed, "
            f"{len(self.skipped)} skipped, "
            f"{self.members_linked} member(s) linked; "
            f"{self.total_groups} active group(s) total."
        )


def _clean_group_name(raw: str | None) -> str | None:
    """A usable group name, or None for a blank/placeholder/punctuation-only cell.

    Only blank-looking, symbol-only, yes/no and blood-group cells are dropped -
    a stray "," or "B+" from the form. Genuinely different spellings ("JSSG" vs
    "JSSG professional") are kept separate; names that differ only by case or
    padding are collapsed by :func:`import_member_groups`.
    """
    name = (raw or "").strip()
    if not name or is_placeholder(name):
        return None
    if not any(char.isalnum() for char in name):
        return None
    if name.casefold() in _NON_GROUP_VALUES or _BLOOD_GROUP.fullmatch(name):
        return None
    return name[:NAME_MAX]


async def _member_group_names(session: AsyncSession) -> list[str]:
    """Distinct non-empty ``profile_data.social_group_name`` values on members."""
    result = await session.execute(
        select(func.distinct(Member.profile_data["social_group_name"].astext)).where(
            Member.is_deleted.is_(False),
            Member.profile_data["social_group_name"].astext.isnot(None),
            Member.profile_data["social_group_name"].astext != "",
        )
    )
    return [name for (name,) in result.all() if name]


async def _link_members_to_groups(session: AsyncSession) -> int:
    """Point members at the group row matching their free-text group name.

    A member is only (re)linked when it has no group yet, or when its current
    ``group_id`` points at a deleted/absent row - a deliberate manual assignment
    to a live group is never overwritten. Matching ignores case and padding.
    """
    groups = (
        await session.execute(
            select(SocialGroup.id, SocialGroup.name).where(
                SocialGroup.is_deleted.is_(False)
            )
        )
    ).all()
    valid_ids = {group_id for group_id, _name in groups}
    by_name = {
        name.strip().casefold(): group_id
        for group_id, name in groups
        if name and name.strip()
    }
    if not by_name:
        return 0

    rows = (
        await session.execute(
            select(
                Member.id,
                Member.group_id,
                Member.profile_data["social_group_name"].astext,
            ).where(
                Member.is_deleted.is_(False),
                Member.profile_data["social_group_name"].astext.isnot(None),
            )
        )
    ).all()

    pending: dict[Any, list[Any]] = {}
    for member_id, group_id, raw_name in rows:
        target = by_name.get((raw_name or "").strip().casefold())
        if target is None:
            continue
        if group_id is not None and group_id in valid_ids:
            continue
        pending.setdefault(target, []).append(member_id)

    linked = 0
    for target, member_ids in pending.items():
        await session.execute(
            update(Member).where(Member.id.in_(member_ids)).values(group_id=target)
        )
        linked += len(member_ids)
    return linked


async def import_member_groups(
    session: AsyncSession,
    *,
    sheet_names: Iterable[str] | None = None,
    include_member_data: bool = True,
    link_members: bool = True,
) -> GroupImportSummary:
    """Save every group name from the sheet and the member directory.

    Creates a ``social_groups`` row for each distinct name that does not exist
    yet (idempotent - case/padding-insensitive against existing rows), then
    links members whose ``profile_data.social_group_name`` matches a group.

    ``sheet_names`` may be passed to avoid the blocking Google Sheets read (used
    by tests and by callers that already fetched the names); ``None`` reads the
    configured sheet, while an empty iterable skips it.
    """
    candidates: dict[str, str] = {}
    skipped: list[str] = []

    def _add(raw: str | None) -> None:
        name = _clean_group_name(raw)
        if name is None:
            if (raw or "").strip():
                skipped.append(raw.strip())
            return
        candidates.setdefault(name.casefold(), name)

    if sheet_names is None:
        sheet_names = await asyncio.to_thread(sheet_social_group_names)
    sheet_values = list(sheet_names)
    for name in sheet_values:
        _add(name)

    member_values: list[str] = []
    if include_member_data:
        member_values = await _member_group_names(session)
        for name in member_values:
            _add(name)

    repo = GroupRepository(session)
    created: list[str] = []
    existing: list[str] = []
    for name in candidates.values():
        found = await repo.get_by_name(name)
        if found is not None:
            existing.append(found.name)
            continue
        await repo.create(name=name, status="ACTIVE", is_active=True)
        created.append(name)
    await session.flush()

    members_linked = await _link_members_to_groups(session) if link_members else 0

    total_groups = (
        await session.execute(
            select(func.count())
            .select_from(SocialGroup)
            .where(SocialGroup.is_deleted.is_(False))
        )
    ).scalar_one()

    return GroupImportSummary(
        sheet_names_read=len(sheet_values),
        member_names_read=len(member_values),
        created=created,
        existing=existing,
        skipped=skipped,
        members_linked=members_linked,
        total_groups=int(total_groups),
    )
