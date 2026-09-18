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
    created_at: datetime
    updated_at: datetime
