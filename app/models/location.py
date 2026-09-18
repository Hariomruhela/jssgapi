from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.group import SocialGroup
    from app.models.member import Member


class Location(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "locations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_hi: Mapped[str | None] = mapped_column(String(255), nullable=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="AREA")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("locations.id", ondelete="CASCADE"),
        nullable=True,
    )
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    area: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pincode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    latitude: Mapped[float | None] = mapped_column(nullable=True)
    longitude: Mapped[float | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    parent: Mapped[Location | None] = relationship(
        remote_side="Location.id", back_populates="children"
    )
    children: Mapped[list[Location]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    groups: Mapped[list[SocialGroup]] = relationship(back_populates="location")
    members: Mapped[list[Member]] = relationship(back_populates="location")

    def __repr__(self) -> str:
        return f"<Location {self.name} ({self.level})>"
