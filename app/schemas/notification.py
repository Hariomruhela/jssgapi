from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DeviceTokenRegister(BaseModel):
    token: str
    platform: str
    device_name: str | None = None


class DeviceTokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    token: str
    platform: str
    device_name: str | None = None
    is_active: bool
    created_at: datetime


class NotificationCreate(BaseModel):
    recipient_id: UUID
    title: str
    body: str | None = None
    notification_type: str = "GENERAL"
    entity_type: str | None = None
    entity_id: UUID | None = None
    data: str | None = None


class NotificationSendRequest(BaseModel):
    recipient_id: UUID
    title: str
    body: str | None = None
    notification_type: str = "GENERAL"
    entity_type: str | None = None
    entity_id: UUID | None = None
    data: str | None = None


class NotificationBroadcastRequest(BaseModel):
    title: str
    body: str | None = None
    notification_type: str = "GENERAL"


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    recipient_id: UUID
    title: str
    body: str | None = None
    data: str | None = None
    notification_type: str
    entity_type: str | None = None
    entity_id: UUID | None = None
    is_read: bool
    read_at: datetime | None = None
    sent_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
