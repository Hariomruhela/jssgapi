from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str | None = None
    phone_number: str | None = None
    full_name: str
    is_active: bool
    is_email_verified: bool
    is_phone_verified: bool
    last_login_at: datetime | None = None
    role: RoleOut | None = None
    # The group this account administers. Non-null for a GROUP_ADMIN and null
    # otherwise, so a client can tell which group an admin is limited to without
    # a second request. Present on the login response for the same reason.
    group_id: UUID | None = None
    group_name: str | None = None
    created_at: datetime
    updated_at: datetime


class UserCreate(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=255)
    phone_number: str | None = None
    role_name: str | None = None
    is_active: bool = True


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=255)
    phone_number: str | None = None
    is_active: bool | None = None
    role_name: str | None = None


class RoleChangeRequest(BaseModel):
    """Request body for ``PATCH /users/{id}/role``.

    ``group_id`` is required when assigning GROUP_ADMIN and ignored otherwise; a
    role without a group clears any group the account previously held.
    """

    model_config = ConfigDict(extra="forbid")

    role_name: str
    group_id: UUID | None = None


class LocalUserOut(UserOut):
    pass
