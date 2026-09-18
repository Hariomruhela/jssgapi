from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class LocationCreate(BaseModel):
    name: str
    name_hi: str | None = None
    level: str = "AREA"
    parent_id: UUID | None = None
    country: str | None = None
    state: str | None = None
    region: str | None = None
    city: str | None = None
    area: str | None = None
    pincode: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_active: bool = True


class LocationUpdate(BaseModel):
    name: str | None = None
    name_hi: str | None = None
    level: str | None = None
    parent_id: UUID | None = None
    country: str | None = None
    state: str | None = None
    region: str | None = None
    city: str | None = None
    area: str | None = None
    pincode: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_active: bool | None = None


class LocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    name_hi: str | None = None
    level: str
    parent_id: UUID | None = None
    country: str | None = None
    state: str | None = None
    region: str | None = None
    city: str | None = None
    area: str | None = None
    pincode: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime
