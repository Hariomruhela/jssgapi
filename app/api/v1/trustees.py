from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.dependencies import DBSession, require_permission
from app.models.trustee import Trustee
from app.repositories.trustee_repository import TrusteeRepository
from app.schemas.trustee import TrusteeCreate, TrusteeOut, TrusteeUpdate

router = APIRouter(prefix="/trustees", tags=["Trustees"])

CREATE = Depends(require_permission(Permission.TRUSTEE_CREATE))
UPDATE = Depends(require_permission(Permission.TRUSTEE_UPDATE))
DELETE = Depends(require_permission(Permission.TRUSTEE_DELETE))


@router.get("")
async def list_trustees(
    db: DBSession,
    group_id: UUID | None = Query(default=None),
    is_current: bool | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = TrusteeRepository(db)
    stmt = select(Trustee)
    stmt = repo.apply_filters(
        stmt,
        {
            k: v
            for k, v in {"group_id": group_id, "is_current": is_current}.items()
            if v is not None
        },
    )
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Trustee.sort_order)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Trustees fetched successfully",
        [TrusteeOut.model_validate(i) for i in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("", dependencies=[CREATE])
async def create_trustee(body: TrusteeCreate, db: DBSession):
    trustee = await TrusteeRepository(db).create(**body.model_dump())
    await db.flush()
    return created("Trustee created successfully", TrusteeOut.model_validate(trustee))


@router.get("/{trustee_id}")
async def get_trustee(trustee_id: UUID, db: DBSession):
    trustee = await TrusteeRepository(db).get_by_id(trustee_id)
    if trustee is None:
        raise NotFoundException("Trustee", str(trustee_id))
    return ok("Trustee fetched successfully", TrusteeOut.model_validate(trustee))


@router.patch("/{trustee_id}", dependencies=[UPDATE])
async def update_trustee(trustee_id: UUID, body: TrusteeUpdate, db: DBSession):
    repo = TrusteeRepository(db)
    trustee = await repo.get_by_id(trustee_id)
    if trustee is None:
        raise NotFoundException("Trustee", str(trustee_id))
    trustee = await repo.update(trustee, **body.model_dump(exclude_unset=True))
    return ok("Trustee updated successfully", TrusteeOut.model_validate(trustee))


@router.delete("/{trustee_id}", dependencies=[DELETE])
async def delete_trustee(trustee_id: UUID, db: DBSession):
    repo = TrusteeRepository(db)
    trustee = await repo.get_by_id(trustee_id)
    if trustee is None:
        raise NotFoundException("Trustee", str(trustee_id))
    await repo.delete(trustee)
    return ok("Trustee deleted successfully")
