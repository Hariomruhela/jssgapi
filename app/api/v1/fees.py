from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.dependencies import DBSession, require_permission
from app.models.fee import Fee
from app.repositories.fee_repository import FeeRepository
from app.schemas.fee import FeeCreate, FeeOut, FeeUpdate

router = APIRouter(prefix="/fees", tags=["Fees"])

READ = Depends(require_permission(Permission.FEE_READ))
MANAGE = Depends(require_permission(Permission.FEE_MANAGE))


@router.get("", dependencies=[READ])
async def list_fees(
    db: DBSession,
    financial_year: str | None = Query(default=None),
    group_id: UUID | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = FeeRepository(db)
    if financial_year:
        items = await repo.list_by_financial_year(financial_year)
        return ok(
            "Fees fetched successfully", [FeeOut.model_validate(f) for f in items]
        )
    if group_id:
        items = await repo.list_by_group(group_id)
        return ok(
            "Fees fetched successfully", [FeeOut.model_validate(f) for f in items]
        )
    stmt = select(Fee)
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Fee.due_date).limit(page_size).offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Fees fetched successfully",
        [FeeOut.model_validate(f) for f in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("", dependencies=[MANAGE])
async def create_fee(body: FeeCreate, db: DBSession):
    fee = await FeeRepository(db).create(**body.model_dump())
    await db.flush()
    return created("Fee created successfully", FeeOut.model_validate(fee))


@router.get("/{fee_id}", dependencies=[READ])
async def get_fee(fee_id: UUID, db: DBSession):
    fee = await FeeRepository(db).get_by_id(fee_id)
    if fee is None:
        raise NotFoundException("Fee", str(fee_id))
    return ok("Fee fetched successfully", FeeOut.model_validate(fee))


@router.patch("/{fee_id}", dependencies=[MANAGE])
async def update_fee(fee_id: UUID, body: FeeUpdate, db: DBSession):
    repo = FeeRepository(db)
    fee = await repo.get_by_id(fee_id)
    if fee is None:
        raise NotFoundException("Fee", str(fee_id))
    fee = await repo.update(fee, **body.model_dump(exclude_unset=True))
    return ok("Fee updated successfully", FeeOut.model_validate(fee))


@router.delete("/{fee_id}", dependencies=[MANAGE])
async def delete_fee(fee_id: UUID, db: DBSession):
    repo = FeeRepository(db)
    fee = await repo.get_by_id(fee_id)
    if fee is None:
        raise NotFoundException("Fee", str(fee_id))
    await repo.delete(fee)
    return ok("Fee deleted successfully")
