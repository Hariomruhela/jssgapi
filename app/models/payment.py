from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import PaymentStatus
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.fee import Fee
    from app.models.member import Member


class Payment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "payments"

    member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("members.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    fee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fees.id", ondelete="SET NULL"), nullable=True
    )
    order_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PaymentStatus.PENDING.value, index=True
    )
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    member: Mapped[Member | None] = relationship()
    fee: Mapped[Fee | None] = relationship(back_populates="payments")
    transactions: Mapped[list[PaymentTransaction]] = relationship(
        back_populates="payment", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Payment {self.order_id} ₹{self.amount} {self.status}>"


class PaymentTransaction(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "payment_transactions"

    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    transaction_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_transaction_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    gateway_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)

    payment: Mapped[Payment] = relationship(back_populates="transactions")

    def __repr__(self) -> str:
        return f"<PaymentTransaction {self.transaction_type} {self.status}>"
