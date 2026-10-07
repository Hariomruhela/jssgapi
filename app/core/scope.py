from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import RoleName, resolve_role_name
from app.core.exceptions import ForbiddenException
from app.models.member import Member

if TYPE_CHECKING:
    from app.models.user import User

UNRESTRICTED_ROLES = {
    RoleName.SUPER_ADMIN,
}

SCOPED_ROLES = {
    RoleName.GROUP_ADMIN,
}


@dataclass(frozen=True)
class AccessScope:
    """What a caller is allowed to see, resolved once per request.

    ``unrestricted`` is only true for SUPER_ADMIN. ``group_id`` limits reads to a
    single social group, and ``member_id`` limits them to a single member row.
    A MEMBER with no group gets ``group_id=None`` *and* ``member_id`` set, which
    is deliberately not the same as ``unrestricted``: they see only themselves
    rather than the whole directory.
    """

    unrestricted: bool
    group_id: UUID | None = None
    member_id: UUID | None = None


async def get_access_scope(db: AsyncSession, user: User) -> AccessScope:
    """Resolve read access for any role.

    Write paths use :func:`get_admin_scope`, which rejects MEMBER outright. Read
    paths use this, because a member legitimately reads their own group.
    """
    if not user.role:
        raise ForbiddenException("No role assigned")
    role = resolve_role_name(user.role.name)
    if role in UNRESTRICTED_ROLES:
        return AccessScope(unrestricted=True)

    result = await db.execute(select(Member).where(Member.user_id == user.id))
    member = result.scalar_one_or_none()
    group_id = user.group_id or (member.group_id if member is not None else None)

    if role in SCOPED_ROLES:
        if group_id is None:
            raise ForbiddenException("Your account is not associated with a group")
        return AccessScope(unrestricted=False, group_id=group_id)

    if role is RoleName.MEMBER:
        return AccessScope(
            unrestricted=False,
            group_id=group_id,
            member_id=member.id if member is not None else None,
        )

    raise ForbiddenException("Operation not allowed for this role")


async def get_admin_scope(db: AsyncSession, user: User) -> UUID | None:
    """Return the group an admin is limited to, or None for unrestricted roles.

    The group is read from ``User.group_id``, which a SUPER_ADMIN sets through
    ``POST /groups/{id}/admins``. ``Member.group_id`` is accepted as a fallback so
    Group Admins provisioned before that column existed keep working.
    """
    if not user.role:
        raise ForbiddenException("No role assigned")
    role = resolve_role_name(user.role.name)
    if role in UNRESTRICTED_ROLES:
        return None
    if role not in SCOPED_ROLES:
        raise ForbiddenException("Operation not allowed for this role")
    if user.group_id is not None:
        return user.group_id
    result = await db.execute(select(Member).where(Member.user_id == user.id))
    member = result.scalar_one_or_none()
    if member is None or member.group_id is None:
        raise ForbiddenException("Your account is not associated with a group")
    return member.group_id


async def assert_group_access(
    db: AsyncSession, user: User, target_group_id: UUID | None
) -> None:
    # SUPER_ADMIN is not group scoped, so a record without a group must not block
    # it - resolve the scope before validating the target.
    scope = await get_admin_scope(db, user)
    if scope is None:
        return
    if target_group_id is None:
        raise ForbiddenException("This record is not associated with a group")
    if target_group_id != scope:
        raise ForbiddenException("Operation is limited to your own group")


async def scope_group_filter(
    db: AsyncSession, user: User, requested_group_id: UUID | None
) -> UUID | None:
    """Resolve the ``group_id`` a list endpoint must filter by.

    Returns ``None`` for unrestricted roles (no filter) and the admin's own group
    for GROUP_ADMIN, ignoring whatever the caller asked for. A GROUP_ADMIN that
    requests a *different* group is rejected rather than silently redirected, so
    a client bug surfaces instead of quietly returning the wrong group's data.
    """
    scope = await get_admin_scope(db, user)
    if scope is None:
        return requested_group_id
    if requested_group_id is not None and requested_group_id != scope:
        raise ForbiddenException("Operation is limited to your own group")
    return scope


async def resolve_managed_group(
    db: AsyncSession, user: User, requested_group_id: UUID | None
) -> UUID | None:
    """Force/validate the group_id used when a scoped admin creates a record."""
    scope = await get_admin_scope(db, user)
    if scope is None:
        return requested_group_id
    if requested_group_id is None:
        return scope
    if requested_group_id != scope:
        raise ForbiddenException("Operation is limited to your own group")
    return scope
