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
    email: str
    phone_number: str | None = None
    full_name: str
    is_active: bool
    is_email_verified: bool
    is_phone_verified: bool
    last_login_at: datetime | None = None
    role: RoleOut | None = None
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


class LocalUserOut(UserOut):
    pass
