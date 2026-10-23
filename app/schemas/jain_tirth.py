from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.constants import EventStatus


class JainTirthCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    location: str | None = None
    cover_image_url: str | None = None
    status: EventStatus = EventStatus.PUBLISHED
    is_public: bool = True
    group_id: UUID | None = None


class JainTirthUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    location: str | None = None
    cover_image_url: str | None = None
    status: EventStatus | None = None
    is_public: bool | None = None
    group_id: UUID | None = None

    @model_validator(mode="after")
    def _finalise(self) -> JainTirthUpdate:
        not_clearable = {"title", "status", "is_public"}
        cleared = not_clearable & {
            name for name in self.model_fields_set if getattr(self, name) is None
        }
        if cleared:
            raise ValueError(f"Cannot be set to null: {', '.join(sorted(cleared))}")
        return self


class JainTirthImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    jain_tirth_id: UUID
    url: str
    position: int
    created_at: datetime


class JainTirthOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str | None = None
    location: str | None = None
    cover_image_url: str | None = None
    status: str
    is_public: bool
    group_id: UUID | None = None
    image_count: int = 0
    created_at: datetime
    updated_at: datetime


class JainTirthDetailOut(JainTirthOut):
    images: list[JainTirthImageOut] = Field(default_factory=list)
