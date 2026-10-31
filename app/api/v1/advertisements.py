from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy import func, select

from app.core.constants import AdvertisementStatus, AuditAction
from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import assert_group_access, get_access_scope, resolve_managed_group
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.advertisement import Advertisement
from app.repositories.advertisement_repository import AdvertisementRepository
from app.schemas.advertisement import (
    AdvertisementApproveRequest,
    AdvertisementCreate,
    AdvertisementOut,
    AdvertisementUpdate,
    SubscriptionDurationOption,
)
from app.services.advertisement_service import (
    SUBSCRIPTION_DURATIONS,
    AdvertisementService,
)
from app.services.audit_service import AuditService

router = APIRouter(prefix="/advertisements", tags=["Advertisements"])

CREATE = Depends(require_permission(Permission.ADVERTISEMENT_CREATE))
APPROVE = Depends(require_permission(Permission.ADVERTISEMENT_APPROVE))


def _now() -> datetime:
    return datetime.now(UTC)


def _to_utc_datetime(val: datetime | date | None) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=UTC)
        return val
    if isinstance(val, date):
        return datetime.combine(val, time.min, tzinfo=UTC)
    return None


@router.get("/subscription-durations")
async def get_subscription_durations():
    """Return available advertisement subscription duration options for the dropdown."""
    return ok(
        "Subscription duration options fetched successfully",
        [
            SubscriptionDurationOption.model_validate(opt)
            for opt in SUBSCRIPTION_DURATIONS
        ],
    )


@router.get("")
async def list_advertisements(
    db: DBSession,
    current_user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    placement: str | None = Query(default=None),
    active_only: bool = Query(default=False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List advertisements with optional filtering by group, placement, and status."""
    scope = await get_access_scope(db, current_user)
    effective_group = scope.group_id if not scope.unrestricted else group_id

    stmt = select(Advertisement).where(Advertisement.is_deleted.is_(False))

    if effective_group is not None:
        stmt = stmt.where(Advertisement.group_id == effective_group)
    if active_only:
        stmt = stmt.where(
            Advertisement.is_active.is_(True),
            Advertisement.status == AdvertisementStatus.APPROVED.value,
        )
    if status is not None:
        stmt = stmt.where(Advertisement.status == status)
    if placement is not None:
        stmt = stmt.where(Advertisement.placement == placement)

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
            "total_pages": (total + page_size - 1) // page_size if total > 0 else 0,
        },
    )


@router.post("", dependencies=[CREATE])
async def create_advertisement(
    body: AdvertisementCreate, current_user: CurrentUser, db: DBSession
):
    """Create a new advertisement.

    Fields from creation form (as shown in screenshot):
    - title (Advertise Title *)
    - group_id (Post By *)
    - start_date (Start Date *)
    - end_date (End Date *)
    - person_name (Person Name *)
    - mobile_number (Mobile Number *)
    - cover_image_url (Cover Image *)
    - large_image_url (Large Image *)
    - amount (Advertisement Pay Amount *) - currently optional
    - subscription_months (Subscription duration dropdown)
    """
    effective_group_id = await resolve_managed_group(db, current_user, body.group_id)

    start_dt = _to_utc_datetime(body.start_date) or _now()
    end_dt = _to_utc_datetime(body.end_date)
    months = (
        body.subscription_months
        if body.subscription_months and body.subscription_months > 0
        else 1
    )
    if end_dt is None:
        end_dt = start_dt + timedelta(days=30 * months)

    person_name = body.person_name or current_user.full_name or "Advertiser"
    mobile_number = body.mobile_number or current_user.phone_number or ""

    media_url = body.media_url or body.cover_image_url or body.large_image_url

    # Payment is currently optional. When created without payment,
    # it defaults to APPROVED and is_active=True.
    initial_status = body.status or AdvertisementStatus.APPROVED.value
    initial_payment_status = body.payment_status or "WAIVED"
    amount_val = (
        Decimal(str(body.amount)) if body.amount is not None else Decimal("0.00")
    )

    ad = await AdvertisementRepository(db).create(
        advertiser_id=current_user.id,
        group_id=effective_group_id,
        title=body.title,
        person_name=person_name,
        mobile_number=mobile_number,
        description=body.description,
        media_url=media_url,
        media_type=body.media_type,
        cover_image_url=body.cover_image_url,
        large_image_url=body.large_image_url,
        amount=amount_val,
        subscription_months=months,
        placement=body.placement,
        start_date=start_dt,
        end_date=end_dt,
        target_url=body.target_url,
        status=initial_status,
        is_active=body.is_active if body.is_active is not None else True,
        payment_status=initial_payment_status,
        payment_id=body.payment_id,
    )
    await db.flush()

    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_CREATE,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
        details={
            "title": ad.title,
            "group_id": str(ad.group_id) if ad.group_id else None,
        },
    )
    await db.flush()
    await db.refresh(ad)
    return created(
        "Advertisement created successfully", AdvertisementOut.model_validate(ad)
    )


@router.post("/form", dependencies=[CREATE])
async def create_advertisement_form(
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
    title: str = Form(...),
    person_name: str | None = Form(default=None),
    mobile_number: str | None = Form(default=None),
    group_id: UUID | None = Form(default=None),
    start_date: str | None = Form(default=None),
    end_date: str | None = Form(default=None),
    subscription_months: int = Form(default=1),
    amount: Decimal | None = Form(default=None),
    description: str | None = Form(default=None),
    target_url: str | None = Form(default=None),
    placement: str = Form(default="HOME_BANNER"),
    cover_image: UploadFile | None = File(default=None),
    large_image: UploadFile | None = File(default=None),
):
    """Create an advertisement via multipart form, with optional images."""
    effective_group_id = await resolve_managed_group(db, current_user, group_id)

    parsed_start: datetime | None = None
    if start_date:
        try:
            parsed_start = datetime.fromisoformat(start_date)
        except ValueError:
            parsed_start = _now()
    start_dt = _to_utc_datetime(parsed_start) or _now()

    parsed_end: datetime | None = None
    if end_date:
        try:
            parsed_end = datetime.fromisoformat(end_date)
        except ValueError:
            parsed_end = None
    end_dt = _to_utc_datetime(parsed_end)
    months = subscription_months if subscription_months > 0 else 1
    if end_dt is None:
        end_dt = start_dt + timedelta(days=30 * months)

    p_name = person_name or current_user.full_name or "Advertiser"
    m_number = mobile_number or current_user.phone_number or ""
    amount_val = amount if amount is not None else Decimal("0.00")

    ad = await AdvertisementRepository(db).create(
        advertiser_id=current_user.id,
        group_id=effective_group_id,
        title=title,
        person_name=p_name,
        mobile_number=m_number,
        description=description,
        placement=placement,
        start_date=start_dt,
        end_date=end_dt,
        target_url=target_url,
        amount=amount_val,
        subscription_months=months,
        status=AdvertisementStatus.APPROVED.value,
        is_active=True,
        payment_status="WAIVED",
    )
    await db.flush()

    ad_service = AdvertisementService(db)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    base_url = str(request.base_url)

    if cover_image and cover_image.filename:
        cover_bytes = await cover_image.read()
        if cover_bytes:
            await ad_service.upload_cover(
                ad,
                file_name=cover_image.filename,
                content=cover_bytes,
                mime_type=cover_image.content_type,
                uploaded_by=current_user.id,
                ip_address=ip,
                user_agent=ua,
                base_url=base_url,
            )

    if large_image and large_image.filename:
        large_bytes = await large_image.read()
        if large_bytes:
            await ad_service.upload_large(
                ad,
                file_name=large_image.filename,
                content=large_bytes,
                mime_type=large_image.content_type,
                uploaded_by=current_user.id,
                ip_address=ip,
                user_agent=ua,
                base_url=base_url,
            )

    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_CREATE,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
        details={
            "title": ad.title,
            "group_id": str(ad.group_id) if ad.group_id else None,
        },
    )
    await db.flush()
    await db.refresh(ad)
    return created(
        "Advertisement created successfully", AdvertisementOut.model_validate(ad)
    )


@router.get("/{advertisement_id}")
async def get_advertisement(advertisement_id: UUID, db: DBSession):
    ad = await AdvertisementRepository(db).get_by_id(advertisement_id)
    if ad is None or ad.is_deleted:
        raise NotFoundException("Advertisement", str(advertisement_id))
    return ok("Advertisement fetched successfully", AdvertisementOut.model_validate(ad))


@router.post("/{advertisement_id}/cover-image", dependencies=[CREATE])
async def upload_advertisement_cover_image(
    advertisement_id: UUID,
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
    file: UploadFile = File(...),
):
    """Upload or replace advertisement cover image (Recommended: 1200x628, Max: 2MB)."""
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await assert_group_access(db, current_user, ad.group_id)

    content = await file.read()
    service = AdvertisementService(db)
    url = await service.upload_cover(
        ad,
        file_name=file.filename,
        content=content,
        mime_type=file.content_type,
        uploaded_by=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        base_url=str(request.base_url),
    )
    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_IMAGE_UPLOAD,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
        details={"field": "cover_image", "url": url},
    )
    await db.flush()
    await db.refresh(ad)
    return created(
        "Cover image uploaded successfully", AdvertisementOut.model_validate(ad)
    )


@router.delete("/{advertisement_id}/cover-image", dependencies=[CREATE])
async def delete_advertisement_cover_image(
    advertisement_id: UUID, current_user: CurrentUser, db: DBSession
):
    """Delete advertisement cover image."""
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await assert_group_access(db, current_user, ad.group_id)

    await AdvertisementService(db).remove_cover(ad)
    await db.flush()
    await db.refresh(ad)
    return ok("Cover image deleted successfully", AdvertisementOut.model_validate(ad))


@router.post("/{advertisement_id}/large-image", dependencies=[CREATE])
async def upload_advertisement_large_image(
    advertisement_id: UUID,
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
    file: UploadFile = File(...),
):
    """Upload or replace advertisement large image (Max: 5MB)."""
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await assert_group_access(db, current_user, ad.group_id)

    content = await file.read()
    service = AdvertisementService(db)
    url = await service.upload_large(
        ad,
        file_name=file.filename,
        content=content,
        mime_type=file.content_type,
        uploaded_by=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        base_url=str(request.base_url),
    )
    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_IMAGE_UPLOAD,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
        details={"field": "large_image", "url": url},
    )
    await db.flush()
    await db.refresh(ad)
    return created(
        "Large image uploaded successfully", AdvertisementOut.model_validate(ad)
    )


@router.delete("/{advertisement_id}/large-image", dependencies=[CREATE])
async def delete_advertisement_large_image(
    advertisement_id: UUID, current_user: CurrentUser, db: DBSession
):
    """Delete advertisement large image."""
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await assert_group_access(db, current_user, ad.group_id)

    await AdvertisementService(db).remove_large(ad)
    await db.flush()
    await db.refresh(ad)
    return ok("Large image deleted successfully", AdvertisementOut.model_validate(ad))


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
    advertisement_id: UUID,
    body: AdvertisementUpdate,
    current_user: CurrentUser,
    db: DBSession,
):
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await assert_group_access(db, current_user, ad.group_id)

    dump = body.model_dump(exclude_unset=True)
    if "start_date" in dump:
        dump["start_date"] = _to_utc_datetime(dump["start_date"])
    if "end_date" in dump:
        dump["end_date"] = _to_utc_datetime(dump["end_date"])
    if "amount" in dump and dump["amount"] is not None:
        dump["amount"] = Decimal(str(dump["amount"]))

    ad = await repo.update(ad, **dump)
    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_UPDATE,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
    )
    await db.flush()
    await db.refresh(ad)
    return ok("Advertisement updated successfully", AdvertisementOut.model_validate(ad))


@router.delete("/{advertisement_id}", dependencies=[CREATE])
async def delete_advertisement(
    advertisement_id: UUID, current_user: CurrentUser, db: DBSession
):
    repo = AdvertisementRepository(db)
    ad = await repo.get_by_id(advertisement_id)
    if ad is None:
        raise NotFoundException("Advertisement", str(advertisement_id))
    await assert_group_access(db, current_user, ad.group_id)

    await AdvertisementService(db).delete_all_for_ad(ad)
    await repo.delete(ad)
    await AuditService(db).log(
        AuditAction.ADVERTISEMENT_DELETE,
        user_id=current_user.id,
        entity_type="advertisement",
        entity_id=ad.id,
    )
    await db.flush()
    return ok("Advertisement deleted successfully")
