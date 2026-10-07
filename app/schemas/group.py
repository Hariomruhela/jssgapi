from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SocialGroupCreate(BaseModel):
    name: str
    name_hi: str | None = None
    description: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    address: str | None = None
    logo_url: str | None = None
    status: str = "ACTIVE"
    is_active: bool = True
    location_id: UUID | None = None


class SocialGroupUpdate(BaseModel):
    name: str | None = None
    name_hi: str | None = None
    description: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    address: str | None = None
    logo_url: str | None = None
    status: str | None = None
    is_active: bool | None = None
    location_id: UUID | None = None


class SocialGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    name_hi: str | None = None
    description: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    address: str | None = None
    logo_url: str | None = None
    status: str
    is_active: bool
    location_id: UUID | None = None
    # Login accounts assigned to administer this group. Empty for a group that has
    # no Group Admin yet, which is a valid state a SUPER_ADMIN must be able to see.
    admins: list[GroupAdminOut] = []
    created_at: datetime
    updated_at: datetime


class GroupAdminOut(BaseModel):
    """A login account that administers a group."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str
    email: str | None = None
    phone_number: str | None = None
    is_active: bool


class GroupAdminAssignment(BaseModel):
    """Request body for ``POST /groups/{id}/admins``.

    ``role_name`` defaults to GROUP_ADMIN. A SUPER_ADMIN may also pass SUPER_ADMIN
    to grant a second platform administrator through the same endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role_name: str = "GROUP_ADMIN"


class GroupMemberOut(BaseModel):
    """A member of a group, as returned by ``GET /groups/{id}/members``."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str | None = None
    first_name: str
    last_name: str
    membership_number: str | None = None
    membership_status: str
    contact_email: str | None = None
    contact_phone: str | None = None
    profile_photo_url: str | None = None


class GroupMemberAssignment(BaseModel):
    """Request body for ``POST /groups/{id}/members``.

    Either ``member_id`` (attach an existing member) or ``user_id`` (promote a
    login account to a member of this group) must be supplied.
    """

    model_config = ConfigDict(extra="forbid")

    member_id: UUID | None = None
    user_id: UUID | None = None

    def model_post_init(self, __context) -> None:
        if self.member_id is None and self.user_id is None:
            raise ValueError("Either member_id or user_id is required")
        if self.member_id is not None and self.user_id is not None:
            raise ValueError("Supply member_id or user_id, not both")
