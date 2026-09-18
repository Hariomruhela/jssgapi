from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AdvertisementCreate(BaseModel):
    title: str
    description: str | None = None
    media_url: str | None = None
    media_type: str = "IMAGE"
    placement: str = "HOME_BANNER"
    start_date: datetime | None = None
    end_date: datetime | None = None
    target_url: str | None = None
    group_id: UUID | None = None


class AdvertisementUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    media_url: str | None = None
    media_type: str | None = None
    placement: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    target_url: str | None = None
    is_active: bool | None = None


class AdvertisementApproveRequest(BaseModel):
    approve: bool
    rejection_reason: str | None = None


class AdvertisementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str | None = None
    media_url: str | None = None
    media_type: str
    placement: str
    start_date: datetime | None = None
    end_date: datetime | None = None
    target_url: str | None = None
    status: str
    is_active: bool
    group_id: UUID | None = None
    advertiser_id: UUID | None = None
    approved_by: UUID | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    created_at: datetime
    updated_at: datetime
