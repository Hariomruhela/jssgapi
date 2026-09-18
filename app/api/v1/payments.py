from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.payment import Payment
from app.repositories.payment_repository import PaymentRepository
from app.schemas.payment import (
    PaymentCreate,
    PaymentOut,
    PaymentVerifyRequest,
)
from app.services.payment_service import PaymentService

router = APIRouter(prefix="/payments", tags=["Payments"])

READ = Depends(require_permission(Permission.PAYMENT_READ))
MANAGE = Depends(require_permission(Permission.PAYMENT_MANAGE))


@router.get("", dependencies=[READ])
async def list_payments(
    db: DBSession,
    member_id: UUID | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = PaymentRepository(db)
    if member_id:
        items = await repo.list_for_member(
            member_id, limit=page_size, offset=(page - 1) * page_size
        )
        return ok(
            "Payments fetched successfully",
            [PaymentOut.model_validate(p) for p in items],
        )
    stmt = select(Payment)
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Payment.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Payments fetched successfully",
        [PaymentOut.model_validate(p) for p in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("")
async def create_payment(body: PaymentCreate, current_user: CurrentUser, db: DBSession):
    service = PaymentService(db)
    payment = await service.create_order(
        member_id=body.member_id,
        fee_id=body.fee_id,
        amount=body.amount,
        currency=body.currency,
    )
    return created(
        "Payment order created successfully", PaymentOut.model_validate(payment)
    )


@router.get("/{payment_id}", dependencies=[READ])
async def get_payment(payment_id: UUID, db: DBSession):
    payment = await PaymentRepository(db).get_by_id(payment_id)
    if payment is None:
        raise NotFoundException("Payment", str(payment_id))
    return ok("Payment fetched successfully", PaymentOut.model_validate(payment))


@router.post("/{payment_id}/verify", dependencies=[MANAGE])
async def verify_payment(
    payment_id: UUID,
    body: PaymentVerifyRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    repo = PaymentRepository(db)
    payment = await repo.get_by_id(payment_id)
    if payment is None:
        raise NotFoundException("Payment", str(payment_id))
    payment = await PaymentService(db).verify(
        payment,
        provider_reference=body.provider_reference,
        status=body.status,
    )
    return ok("Payment verified successfully", PaymentOut.model_validate(payment))
