from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class SocialGroupCreate(BaseModel):
    """``POST /groups`` body.

    ``state`` and ``city`` are mandatory and are validated against the backend's
    LGD-sourced geo master data - they are what the generated Group ID
    (``MP09001``) is built from. A client-sent ``group_id`` is accepted but
    **ignored**: the server always generates the ID, so a stale or forged value
    can never end up on the record.
    """

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
    state: str
    city: str
    group_id: str | None = None

    @field_validator("state", "city")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """Trim, then refuse missing/empty/whitespace-only values (422)."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped


class SocialGroupUpdate(BaseModel):
    """``PATCH /groups/{id}`` body.

    Deliberately free of ``group_id``, ``state`` and ``city``: the generated ID
    encodes the city a group was created in, so it survives every update
    untouched rather than drifting out of sync with the record.
    """

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
    group_id: str | None = None
    name: str
    state: str | None = None
    city: str | None = None
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
