from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.constants import AuditAction, RoleName, resolve_role_name
from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import (
    assert_group_access,
    get_access_scope,
    get_admin_scope,
)
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.group import SocialGroup
from app.models.member import Member
from app.models.user import Role
from app.repositories.group_repository import GroupRepository
from app.repositories.member_repository import MemberRepository
from app.repositories.user_repository import UserRepository
from app.schemas.group import (
    GroupAdminAssignment,
    GroupAdminOut,
    GroupMemberAssignment,
    GroupMemberOut,
    SocialGroupCreate,
    SocialGroupOut,
    SocialGroupUpdate,
)
from app.services.audit_service import AuditService
from app.services.sheet_groups_service import read_sheet_groups

router = APIRouter(prefix="/groups", tags=["Groups"])

READ = Depends(require_permission(Permission.GROUP_READ))
CREATE = Depends(require_permission(Permission.GROUP_CREATE))
UPDATE = Depends(require_permission(Permission.GROUP_UPDATE))
DELETE = Depends(require_permission(Permission.GROUP_DELETE))
# A GROUP_ADMIN must be able to add and remove members of its own group, which is
# why membership management is not folded into GROUP_UPDATE (a group-settings edit).
MANAGE_MEMBERS = Depends(require_permission(Permission.GROUP_MEMBER_MANAGE))
ASSIGN_ADMIN = Depends(require_permission(Permission.ROLE_MANAGE))


async def _resolve_role(db, role_name: str | None) -> Role:
    """Resolve a role label to its ``roles`` row.

    ``resolve_role_name`` accepts the retired labels and folds them onto a
    surviving role, so a stale client that still sends "ADMIN" grants
    SUPER_ADMIN rather than a role that no longer exists.
    """
    canonical = resolve_role_name(role_name)
    if canonical is None:
        raise BadRequestException(f"Role '{role_name}' does not exist")
    result = await db.execute(select(Role).where(Role.name == canonical.value))
    role = result.scalar_one_or_none()
    if role is None:
        raise BadRequestException(f"Role '{canonical.value}' is not configured yet")
    return role


@router.get("", dependencies=[READ])
async def list_groups(
    db: DBSession,
    current_user: CurrentUser,
    query: str | None = Query(default=None, max_length=200),
    status: str | None = Query(default=None),
    source: str = Query(
        default="db",
        pattern="^(db|sheets)$",
        description="db = social_groups table, sheets = distinct names on the "
        "registration Google Sheet",
    ),
    field: str | None = Query(
        default=None,
        pattern="^(social_group_name|group_designation)$",
        description="Sheet column to read; source=sheets only",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Groups, either the stored ``social_groups`` rows or the free-text group
    names read from the registration sheet (``source=sheets``).

    The sheet source answers with ``{"id", "name", "field"}`` items, is
    de-duplicated and sorted by name, and ignores ``status``/paging - it is a
    picker's option list, not a paged resource. Both sources need
    ``group:read``; a GROUP_ADMIN still sees only its own group in the database
    source.
    """
    if source == "sheets":
        # Reading the sheet is a network call on Google's side, so it runs in a
        # worker thread instead of blocking the event loop.
        sheet_items = await asyncio.to_thread(read_sheet_groups)
        if field is not None:
            sheet_items = [i for i in sheet_items if i["field"] == field]
        if query:
            needle = query.strip().casefold()
            sheet_items = [i for i in sheet_items if needle in i["name"].casefold()]
        return ok("Groups fetched successfully", sheet_items)

    repo = GroupRepository(db)
    scope = await get_access_scope(db, current_user)
    # A GROUP_ADMIN sees only their own group. Without this, the panel's group
    # picker would list every group and the search box would return all of them.
    visible_group = None if scope.unrestricted else scope.group_id
    if query:
        groups = await repo.search(
            query,
            group_id=visible_group,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return ok(
            "Groups fetched successfully",
            [SocialGroupOut.model_validate(g) for g in groups],
        )
    stmt = select(SocialGroup)
    filters: dict[str, Any] = {
        k: v for k, v in {"status": status}.items() if v is not None
    }
    if visible_group is not None:
        filters["id"] = visible_group
    stmt = repo.apply_filters(stmt, filters)
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(SocialGroup.name).limit(page_size).offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Groups fetched successfully",
        [SocialGroupOut.model_validate(i) for i in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("", dependencies=[CREATE])
async def create_group(body: SocialGroupCreate, db: DBSession):
    repo = GroupRepository(db)
    existing = await repo.get_by_name(body.name)
    if existing:
        return created(
            "Group created successfully", SocialGroupOut.model_validate(existing)
        )
    group = await repo.create(**body.model_dump())
    await db.flush()
    return created("Group created successfully", SocialGroupOut.model_validate(group))


@router.get("/{group_id}", dependencies=[READ])
async def get_group(group_id: UUID, db: DBSession, current_user: CurrentUser):
    group = await GroupRepository(db).get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    scope = await get_access_scope(db, current_user)
    if not scope.unrestricted and scope.group_id != group_id:
        raise NotFoundException("Group", str(group_id))
    return ok("Group fetched successfully", SocialGroupOut.model_validate(group))


@router.get("/{group_id}/members", dependencies=[READ])
async def list_group_members(
    group_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Members of a group. This is how a Group Admin's directory stays group-bound."""
    group = await GroupRepository(db).get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    scope = await get_access_scope(db, current_user)
    if not scope.unrestricted and scope.group_id != group_id:
        # Reported as missing rather than forbidden, matching GET /groups/{id}.
        raise NotFoundException("Group", str(group_id))

    stmt = select(Member).where(
        Member.group_id == group_id, Member.is_deleted.is_(False)
    )
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Member.first_name).limit(page_size).offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Group members fetched successfully",
        [_group_member_out(m) for m in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


def _group_member_out(member: Member) -> GroupMemberOut:
    """``full_name`` lives in ``profile_data`` for user-less members, so fall back."""
    full_name = (member.profile_data or {}).get("full_name")
    if not full_name and member.user is not None:
        full_name = member.user.full_name
    if not full_name:
        full_name = " ".join(
            part
            for part in (member.first_name, member.middle_name, member.last_name)
            if part
        )
    return GroupMemberOut(
        id=member.id,
        full_name=full_name,
        first_name=member.first_name,
        last_name=member.last_name,
        membership_number=member.membership_number,
        membership_status=member.membership_status,
        contact_email=member.contact_email,
        contact_phone=member.contact_phone,
        profile_photo_url=member.profile_photo_url,
    )


@router.post("/{group_id}/admins", dependencies=[ASSIGN_ADMIN])
async def assign_group_admin(group_id: UUID, body: GroupAdminAssignment, db: DBSession):
    """Grant a login account administration of this group.

    Sets ``User.group_id`` (the scope source of truth in ``app/core/scope.py``)
    and assigns the role. Reassigning the same user to the same group is a no-op
    rather than an error, so the panel can retry safely.
    """
    repo = GroupRepository(db)
    group = await repo.get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))

    role = await _resolve_role(db, body.role_name)
    user_repo = UserRepository(db)
    user = await user_repo.get_by_id(body.user_id)
    if user is None:
        raise NotFoundException("User", str(body.user_id))
    if not user.is_active:
        raise BadRequestException("Cannot assign an inactive account as a Group Admin")

    user.role_id = role.id
    user.group_id = group_id
    await db.flush()
    await db.refresh(user)
    await AuditService(db).log(
        AuditAction.ROLE_CHANGE,
        entity_type="user",
        entity_id=user.id,
        details={"role": role.name, "group_id": str(group_id)},
    )
    return ok(
        "Group Admin assigned successfully",
        {
            "group": SocialGroupOut.model_validate(group),
            "admin": GroupAdminOut.model_validate(user),
        },
    )


@router.delete("/{group_id}/admins/{user_id}", dependencies=[ASSIGN_ADMIN])
async def remove_group_admin(group_id: UUID, user_id: UUID, db: DBSession):
    """Revoke a login account's administration of this group.

    Demotes the account to MEMBER and clears ``group_id`` so it loses all group
    scope immediately, rather than leaving a stale scope behind.
    """
    group = await GroupRepository(db).get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    user = await UserRepository(db).get_by_id(user_id)
    if user is None:
        raise NotFoundException("User", str(user_id))
    if user.group_id != group_id:
        raise NotFoundException("Group Admin assignment", str(user_id))

    role = await _resolve_role(db, RoleName.MEMBER.value)
    user.role_id = role.id
    user.group_id = None
    await db.flush()
    await db.refresh(user)
    await AuditService(db).log(
        AuditAction.ROLE_CHANGE,
        entity_type="user",
        entity_id=user.id,
        details={"role": RoleName.MEMBER.value, "removed_from_group": str(group_id)},
    )
    return ok("Group Admin removed successfully", GroupAdminOut.model_validate(user))


@router.post("/{group_id}/members", dependencies=[MANAGE_MEMBERS])
async def add_group_member(
    group_id: UUID,
    body: GroupMemberAssignment,
    db: DBSession,
    current_user: CurrentUser,
):
    """Attach an existing member (or a login account) to this group.

    ``member_id`` moves an existing member profile into the group. ``user_id``
    creates the member profile if the account does not have one yet, so a newly
    registered member can be added to a group without a second step.
    """
    group = await GroupRepository(db).get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    # A GROUP_ADMIN may add members to their own group only.
    await assert_group_access(db, current_user, group_id)

    member_repo = MemberRepository(db)
    if body.member_id is not None:
        member = await member_repo.get_by_id(body.member_id)
        if member is None:
            raise NotFoundException("Member", str(body.member_id))
    else:
        assert body.user_id is not None
        member = await member_repo.get_by_user_id(body.user_id)
        if member is None:
            user = await UserRepository(db).get_by_id(body.user_id)
            if user is None:
                raise NotFoundException("User", str(body.user_id))
            name_parts = user.full_name.split()
            member = await member_repo.create(
                user_id=user.id,
                first_name=name_parts[0] if name_parts else user.full_name,
                last_name=name_parts[-1] if len(name_parts) > 1 else "",
                contact_phone=user.phone_number,
                contact_email=user.email,
            )

    if member.group_id == group_id:
        return ok("Member is already in this group", _group_member_out(member))
    # A scoped caller may only take a member that belongs to another group. An
    # unassigned member (group_id IS NULL) is fair game, but moving someone out of
    # a group this caller does not run would be a cross-group write.
    scope = await get_admin_scope(db, current_user)
    if scope is not None and member.group_id is not None and member.group_id != scope:
        raise ForbiddenException("Operation is limited to your own group")
    member.group_id = group_id
    await db.flush()
    await db.refresh(member)
    await AuditService(db).log(
        AuditAction.GROUP_UPDATE,
        entity_type="member",
        entity_id=member.id,
        details={"group_id": str(group_id), "action": "member_added"},
    )
    return created("Member added to group", _group_member_out(member))


@router.delete("/{group_id}/members/{member_id}", dependencies=[MANAGE_MEMBERS])
async def remove_group_member(
    group_id: UUID, member_id: UUID, db: DBSession, current_user: CurrentUser
):
    """Remove a member from this group.

    The member profile itself is kept; only the group link is cleared, so no
    family, professional or profile data is destroyed.
    """
    group = await GroupRepository(db).get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    await assert_group_access(db, current_user, group_id)
    member = await MemberRepository(db).get_by_id(member_id)
    if member is None or member.group_id != group_id:
        raise NotFoundException("Member", str(member_id))

    member.group_id = None
    await db.flush()
    await AuditService(db).log(
        AuditAction.GROUP_UPDATE,
        entity_type="member",
        entity_id=member.id,
        details={"group_id": str(group_id), "action": "member_removed"},
    )
    return ok("Member removed from group")


@router.patch("/{group_id}", dependencies=[UPDATE])
async def update_group(
    group_id: UUID, body: SocialGroupUpdate, current_user: CurrentUser, db: DBSession
):
    repo = GroupRepository(db)
    group = await repo.get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    await assert_group_access(db, current_user, group.id)
    group = await repo.update(group, **body.model_dump(exclude_unset=True))
    await AuditService(db).log(
        AuditAction.GROUP_UPDATE, entity_type="group", entity_id=group.id
    )
    await db.flush()
    return ok("Group updated successfully", SocialGroupOut.model_validate(group))


@router.delete("/{group_id}", dependencies=[DELETE])
async def delete_group(group_id: UUID, current_user: CurrentUser, db: DBSession):
    repo = GroupRepository(db)
    group = await repo.get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    await assert_group_access(db, current_user, group.id)
    await repo.delete(group)
    await AuditService(db).log(
        AuditAction.GROUP_DELETE, entity_type="group", entity_id=group.id
    )
    await db.flush()
    return ok("Group deleted successfully")
