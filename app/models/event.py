from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.group import SocialGroup
    from app.models.location import Location


class Event(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "events"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    title_hi: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    venue_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    venue_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PUBLISHED", index=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_attendees: Mapped[int | None] = mapped_column(nullable=True)
    registration_required: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_groups.id", ondelete="SET NULL"),
        nullable=True,
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
    )

    group: Mapped[SocialGroup | None] = relationship()
    location: Mapped[Location | None] = relationship()

    def __repr__(self) -> str:
        return f"<Event {self.title}>"
