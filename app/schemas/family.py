from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class FamilyMemberCreate(BaseModel):
    name: str
    relationship_type: str
    date_of_birth: date | None = None
    occupation: str | None = None
    contact_phone: str | None = None
    is_visible: bool = True


class FamilyMemberUpdate(BaseModel):
    name: str | None = None
    relationship_type: str | None = None
    date_of_birth: date | None = None
    occupation: str | None = None
    contact_phone: str | None = None
    is_visible: bool | None = None


class FamilyMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    member_id: UUID
    name: str
    relationship_type: str
    date_of_birth: date | None = None
    occupation: str | None = None
    contact_phone: str | None = None
    is_visible: bool
    is_approved: bool
    created_at: datetime
    updated_at: datetime
