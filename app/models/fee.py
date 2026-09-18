from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import FeeStatus
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.group import SocialGroup
    from app.models.payment import Payment


class Fee(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "fees"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_groups.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    financial_year: Mapped[str] = mapped_column(String(9), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FeeStatus.PENDING.value, index=True
    )
    is_waived: Mapped[bool] = mapped_column(default=False, nullable=False)

    group: Mapped[SocialGroup | None] = relationship()
    payments: Mapped[list[Payment]] = relationship(back_populates="fee")

    def __repr__(self) -> str:
        return f"<Fee {self.title} FY{self.financial_year} ₹{self.amount}>"
