from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.core.constants import NewsStatus
from app.schemas.user import UserOut


class NewsCreate(BaseModel):
    title: str
    title_hi: str | None = None
    summary: str | None = None
    content: str | None = None
    cover_image_url: str | None = None
    status: NewsStatus = NewsStatus.DRAFT
    scheduled_at: datetime | None = None
    is_featured: bool = False


class NewsUpdate(BaseModel):
    title: str | None = None
    title_hi: str | None = None
    summary: str | None = None
    content: str | None = None
    cover_image_url: str | None = None
    status: NewsStatus | None = None
    scheduled_at: datetime | None = None
    is_featured: bool | None = None


class NewsPublishRequest(BaseModel):
    status: NewsStatus = NewsStatus.PUBLISHED


class NewsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    title_hi: str | None = None
    summary: str | None = None
    content: str | None = None
    cover_image_url: str | None = None
    status: str
    published_at: datetime | None = None
    scheduled_at: datetime | None = None
    is_featured: bool
    author_id: UUID | None = None
    author: UserOut | None = None
    created_at: datetime
    updated_at: datetime
