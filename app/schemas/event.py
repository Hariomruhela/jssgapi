from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.core.constants import EventStatus


class EventCreate(BaseModel):
    title: str
    title_hi: str | None = None
    description: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    venue_name: str | None = None
    venue_address: str | None = None
    cover_image_url: str | None = None
    status: EventStatus = EventStatus.PUBLISHED
    is_public: bool = True
    max_attendees: int | None = None
    registration_required: bool = False
    group_id: UUID | None = None
    location_id: UUID | None = None


class EventUpdate(BaseModel):
    title: str | None = None
    title_hi: str | None = None
    description: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    venue_name: str | None = None
    venue_address: str | None = None
    cover_image_url: str | None = None
    status: EventStatus | None = None
    is_public: bool | None = None
    max_attendees: int | None = None
    registration_required: bool | None = None
    group_id: UUID | None = None
    location_id: UUID | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    title_hi: str | None = None
    description: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    venue_name: str | None = None
    venue_address: str | None = None
    cover_image_url: str | None = None
    status: str
    is_public: bool
    max_attendees: int | None = None
    registration_required: bool
    group_id: UUID | None = None
    location_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
