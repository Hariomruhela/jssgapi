from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.constants import AdvertisementStatus, AuditAction
from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.advertisement import Advertisement
from app.repositories.advertisement_repository import AdvertisementRepository
from app.schemas.advertisement import (
    AdvertisementApproveRequest,
    AdvertisementCreate,
    AdvertisementOut,
    AdvertisementUpdate,
)
from app.services.audit_service import AuditService

router = APIRouter(prefix="/advertisements", tags=["Advertisements"])

CREATE = Depends(require_permission(Permission.ADVERTISEMENT_CREATE))
APPROVE = Depends(require_permission(Permission.ADVERTISEMENT_APPROVE))


def _now() -> datetime:
    return datetime.now(UTC)


@router.get("")
async def list_advertisements(
    db: DBSession,
    placement: str | None = Query(default=None),
    active_only: bool = Query(default=False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = AdvertisementRepository(db)
    if active_only:
        items = await repo.list_active(limit=page_size)
        return ok(
            "Active advertisements fetched successfully",
            [AdvertisementOut.model_validate(a) for a in items],
        )
    if placement:
        items = await repo.list_by_placement(placement)
        return ok(
            "Advertisements fetched successfully",
            [AdvertisementOut.model_validate(a) for a in items],
        )
    stmt = select(Advertisement)
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Advertisement.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Advertisements fetched successfully",
        [AdvertisementOut.model_validate(a) for a in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("", dependencies=[CREATE])
async def create_advertisement(
    body: AdvertisementCreate, current_user: CurrentUser, db: DBSession
):
    ad = await AdvertisementRepository(db).create(
        advertiser_id=current_user.id, **body.model_dump()
    )
    await db.flush()
    return created(
        "Advertisement created successfully", AdvertisementOut.model_validate(ad)
    )


@router.get("/{advertisement_id}")
async def get_advertisement(advertisement_id: UUID, db: DBSession):
    ad = await AdvertisementRepository(db).get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    return ok("Advertisement fetched successfully", AdvertisementOut.model_validate(ad))


@router.post("/{advertisement_id}/approve", dependencies=[APPROVE])
async def approve_advertisement(
    advertisement_id: UUID,
    body: AdvertisementApproveRequest,
    current_user: CurrentUser,
    db: DBSession,
):
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    if body.approve:
        ad.status = AdvertisementStatus.APPROVED.value
        ad.approved_by = current_user.id
        ad.approved_at = _now()
        ad.rejection_reason = None
    else:
        ad.status = AdvertisementStatus.REJECTED.value
        ad.rejection_reason = body.rejection_reason or "Rejected by admin"
    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_APPROVE,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
    )
    await db.flush()
    await db.refresh(ad)
    return ok(
        "Advertisement status updated successfully", AdvertisementOut.model_validate(ad)
    )


@router.patch("/{advertisement_id}", dependencies=[CREATE])
async def update_advertisement(
    advertisement_id: UUID, body: AdvertisementUpdate, db: DBSession
):
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    ad = await repo.update(ad, **body.model_dump(exclude_unset=True))
    return ok("Advertisement updated successfully", AdvertisementOut.model_validate(ad))


@router.delete("/{advertisement_id}", dependencies=[CREATE])
async def delete_advertisement(advertisement_id: UUID, db: DBSession):
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await repo.delete(ad)
    return ok("Advertisement deleted successfully")
