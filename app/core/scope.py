from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import RoleName
from app.core.exceptions import ForbiddenException
from app.models.member import Member

if TYPE_CHECKING:
    from app.models.user import User

UNRESTRICTED_ROLES = {
    RoleName.SUPER_ADMIN.value,
    RoleName.FEDERATION_ADMIN.value,
    RoleName.ADMIN.value,
}

SCOPED_ROLES = {
    RoleName.REGIONAL_ADMIN.value,
    RoleName.GROUP_ADMIN.value,
}


async def get_admin_scope(db: AsyncSession, user: User) -> UUID | None:
    """Return the group an admin is limited to, or None for unrestricted roles."""
    if not user.role:
        raise ForbiddenException("No role assigned")
    role = user.role.name
    if role in UNRESTRICTED_ROLES:
        return None
    if role not in SCOPED_ROLES:
        raise ForbiddenException("Operation not allowed for this role")
    result = await db.execute(select(Member).where(Member.user_id == user.id))
    member = result.scalar_one_or_none()
    if member is None or member.group_id is None:
        raise ForbiddenException("Your account is not associated with a group")
    return member.group_id


async def assert_group_access(
    db: AsyncSession, user: User, target_group_id: UUID | None
) -> None:
    if target_group_id is None:
        raise ForbiddenException("This record is not associated with a group")
    scope = await get_admin_scope(db, user)
    if scope is None:
        return
    if target_group_id != scope:
        raise ForbiddenException("Operation is limited to your own group")


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
