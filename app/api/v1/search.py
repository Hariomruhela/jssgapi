from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.v1.members import _member_list_item
from app.core.exceptions import BadRequestException, ForbiddenException
from app.core.permissions import Permission, check_permission
from app.core.responses import ok
from app.core.scope import get_access_scope
from app.dependencies import CurrentUser, DBSession
from app.repositories.group_repository import GroupRepository
from app.repositories.member_repository import MemberRepository
from app.schemas.search import GroupSearchItemOut
from app.services.profile_service import ProfileService
from app.utils.helpers import sanitize_search_query

router = APIRouter(prefix="/search", tags=["Search"])


def _pagination(page: int, page_size: int, total: int) -> dict[str, int]:
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.get("")
async def search(
    db: DBSession,
    current_user: CurrentUser,
    q: str = Query(..., min_length=1, max_length=200),
    type: str = Query(default="all", pattern="^(all|members|groups)$"),
    group_id: UUID | None = Query(default=None),
    group_name: str | None = Query(default=None, max_length=100),
    location: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Keyword search across members and groups.

    A single ``q`` searches every field: member names, profile fields, email,
    phone, membership number, group name and location for members; group name,
    description, contact details, location and its members for groups. A
    keyword that is a UUID also matches ``members.id``/``social_groups.id``,
    and a membership number resolves to its member. ``type`` limits the result
    to one section. Every hit is scope-checked: a GROUP_ADMIN or MEMBER only
    ever sees their own group.
    """
    keyword = sanitize_search_query(q)
    if not keyword:
        raise BadRequestException("Search query is empty", "INVALID_QUERY")

    want_members = type in ("all", "members")
    want_groups = type in ("all", "groups")
    if want_members and not check_permission(current_user, Permission.MEMBER_READ):
        raise ForbiddenException("Missing permission: member.read")
    if want_groups and not check_permission(current_user, Permission.GROUP_READ):
        raise ForbiddenException("Missing permission: group.read")

    scope = await get_access_scope(db, current_user)
    group_name = (group_name or "").strip() or None
    location = (location or "").strip() or None

    # A caller-supplied group_id may only narrow the result, never widen it.
    if group_id is not None and not scope.unrestricted and group_id != scope.group_id:
        raise ForbiddenException("Operation is limited to your own group")
    if group_id is not None:
        effective_group = group_id
    elif scope.unrestricted:
        effective_group = None
    else:
        effective_group = scope.group_id

    data: dict[str, Any] = {}

    if want_members:
        # A MEMBER with no group may still search, but only ever sees itself.
        member_id_filter = (
            scope.member_id
            if (not scope.unrestricted and scope.group_id is None)
            else None
        )
        members, total = await MemberRepository(db).search_detailed(
            keyword,
            group_id=effective_group,
            membership_status=status,
            group_name=group_name,
            location=location,
            member_id=member_id_filter,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        profiles = ProfileService(db)
        data["members"] = {
            "items": [_member_list_item(m, profiles) for m in members],
            "pagination": _pagination(page, page_size, total),
        }

    if want_groups:
        # A scoped caller with no group sees no groups; never fall back to
        # unrestricted when the scope is empty.
        groups_blocked = not scope.unrestricted and scope.group_id is None
        visible_group = None if scope.unrestricted else scope.group_id
        if groups_blocked:
            data["groups"] = {
                "items": [],
                "pagination": _pagination(page, page_size, 0),
            }
        else:
            rows, total = await GroupRepository(db).search_detailed(
                keyword,
                group_id=visible_group,
                status=status,
                location=location,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
            items = []
            for group, location_name, member_count in rows:
                item = GroupSearchItemOut.model_validate(group)
                item.location_name = location_name
                item.member_count = member_count
                items.append(item)
            data["groups"] = {
                "items": items,
                "pagination": _pagination(page, page_size, total),
            }

    payload = ok("Search completed successfully", data)
    payload["query"] = {
        "q": keyword,
        "type": type,
        "group_id": str(group_id) if group_id is not None else None,
        "group_name": group_name,
        "location": location,
        "status": status,
    }
    return payload
