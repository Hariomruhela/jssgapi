from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TrusteeCreate(BaseModel):
    user_id: UUID | None = None
    group_id: UUID
    first_name: str
    last_name: str
    designation: str
    contact_email: str | None = None
    contact_phone: str | None = None
    profile_photo_url: str | None = None
    term_start: datetime | None = None
    term_end: datetime | None = None
    is_active: bool = True
    is_current: bool = True
    sort_order: int = 0


class TrusteeUpdate(BaseModel):
    user_id: UUID | None = None
    group_id: UUID | None = None
    first_name: str | None = None
    last_name: str | None = None
    designation: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    profile_photo_url: str | None = None
    term_start: datetime | None = None
    term_end: datetime | None = None
    is_active: bool | None = None
    is_current: bool | None = None
    sort_order: int | None = None


class TrusteeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID | None = None
    group_id: UUID | None = None
    first_name: str
    last_name: str
    designation: str
    contact_email: str | None = None
    contact_phone: str | None = None
    profile_photo_url: str | None = None
    term_start: datetime | None = None
    term_end: datetime | None = None
    is_active: bool
    is_current: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime
