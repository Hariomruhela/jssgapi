from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.location import Location
    from app.models.member import Member
    from app.models.trustee import Trustee


class SocialGroup(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "social_groups"

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name_hi: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("locations.id", ondelete="SET NULL"),
        nullable=True,
    )

    location: Mapped[Location | None] = relationship(back_populates="groups")
    members: Mapped[list[Member]] = relationship(back_populates="group")
    trustees: Mapped[list[Trustee]] = relationship(back_populates="group")

    def __repr__(self) -> str:
        return f"<SocialGroup {self.name}>"
