from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.constants import AuditAction, PaymentStatus
from app.core.exceptions import BadRequestException, NotFoundException
from app.models.member import Member
from app.models.payment import Payment
from app.repositories.payment_repository import (
    PaymentRepository,
    PaymentTransactionRepository,
)
from app.services.audit_service import AuditService

settings = get_settings()


class PaymentService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.payments = PaymentRepository(session)
        self.transactions = PaymentTransactionRepository(session)
        self.audit = AuditService(session)

    async def create_order(
        self,
        *,
        member_id: uuid.UUID | None,
        fee_id: uuid.UUID | None,
        amount: Decimal,
        currency: str = "INR",
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> Payment:
        if member_id is not None:
            result = await self.session.execute(
                select(Member).where(Member.id == member_id)
            )
            if result.scalar_one_or_none() is None:
                raise NotFoundException("Member", str(member_id))

        order_id = (
            f"{settings.payment_provider or 'JSSG'}-{uuid.uuid4().hex.upper()[:16]}"  # type: ignore[operator]
        )

        payment = await self.payments.create(
            member_id=member_id,
            fee_id=fee_id,
            order_id=order_id,
            amount=amount,
            currency=currency,
            status=PaymentStatus.PENDING.value,
            provider=settings.payment_provider or "dev",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        await self.transactions.create_for_payment(
            payment, transaction_type="ORDER_CREATED", status="PENDING"
        )
        await self.audit.log(
            AuditAction.PAYMENT_CREATE,
            entity_type="payment",
            entity_id=payment.id,
            user_id=None,
            details={"order_id": order_id, "amount": str(amount)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        return payment

    async def verify(
        self,
        payment: Payment,
        *,
        provider_reference: str,
        status: PaymentStatus,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> Payment:
        if payment.status != PaymentStatus.PENDING.value:
            raise BadRequestException(f"Cannot verify a {payment.status} payment")

        final = status if status == PaymentStatus.SUCCESS else PaymentStatus.FAILED
        payment.status = final.value
        payment.provider_reference = provider_reference
        if final == PaymentStatus.SUCCESS:
            payment.paid_at = datetime.now(UTC)

        await self.transactions.create_for_payment(
            payment,
            transaction_type="PAYMENT_VERIFY",
            status=final.value,
            provider_transaction_id=provider_reference,
            amount=payment.amount,
        )
        await self.audit.log(
            AuditAction.PAYMENT_VERIFY,
            entity_type="payment",
            entity_id=payment.id,
            details={
                "order_id": payment.order_id,
                "status": final.value,
                "reference": provider_reference,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        await self.session.refresh(payment)
        return payment
