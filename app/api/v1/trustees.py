from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import (
    assert_group_access,
    get_access_scope,
    resolve_managed_group,
)
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.trustee import Trustee
from app.repositories.member_repository import MemberRepository
from app.repositories.trustee_repository import TrusteeRepository
from app.schemas.trustee import TrusteeCreate, TrusteeOut, TrusteeUpdate

router = APIRouter(prefix="/trustees", tags=["Trustees"])

READ = Depends(require_permission(Permission.TRUSTEE_READ))
CREATE = Depends(require_permission(Permission.TRUSTEE_CREATE))
UPDATE = Depends(require_permission(Permission.TRUSTEE_UPDATE))
DELETE = Depends(require_permission(Permission.TRUSTEE_DELETE))


@router.get("", dependencies=[READ])
async def list_trustees(
    db: DBSession,
    current_user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    is_current: bool | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = TrusteeRepository(db)
    scope = await get_access_scope(db, current_user)
    if group_id is not None and not scope.unrestricted and group_id != scope.group_id:
        raise ForbiddenException("Operation is limited to your own group")
    stmt = select(Trustee)
    filters: dict[str, Any] = {
        k: v for k, v in {"is_current": is_current}.items() if v is not None
    }
    if scope.unrestricted:
        if group_id is not None:
            filters["group_id"] = group_id
    else:
        # Trustees are always read group-scoped: the board belongs to a group.
        filters["group_id"] = scope.group_id
    stmt = repo.apply_filters(stmt, filters)
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
async def create_trustee(body: TrusteeCreate, current_user: CurrentUser, db: DBSession):
    data = body.model_dump()
    data["group_id"] = await resolve_managed_group(db, current_user, body.group_id)
    if data["group_id"] is None:
        raise BadRequestException("A trustee must belong to a group")

    # A linked member must belong to the same group as the board, otherwise the
    # trustee record and the member profile would disagree about whose group this is.
    if body.member_id is not None:
        member = await MemberRepository(db).get_by_id(body.member_id)
        if member is None:
            raise NotFoundException("Member", str(body.member_id))
        if member.group_id != data["group_id"]:
            raise BadRequestException(
                "That member does not belong to this group"
            )
        if not data["first_name"]:
            data["first_name"] = member.first_name
        if not data["last_name"]:
            data["last_name"] = member.last_name
        data["profile_photo_url"] = (
            data["profile_photo_url"] or member.profile_photo_url
        )

    trustee = await TrusteeRepository(db).create(**data)
    await db.flush()
    return created("Trustee created successfully", TrusteeOut.model_validate(trustee))


@router.get("/{trustee_id}", dependencies=[READ])
async def get_trustee(trustee_id: UUID, db: DBSession, current_user: CurrentUser):
    repo = TrusteeRepository(db)
    trustee = await repo.get_by_id(trustee_id)
    if trustee is None:
        raise NotFoundException("Trustee", str(trustee_id))
    scope = await get_access_scope(db, current_user)
    # Reported as missing rather than forbidden so a cross-group id is not
    # confirmed to exist.
    if not scope.unrestricted and trustee.group_id != scope.group_id:
        raise NotFoundException("Trustee", str(trustee_id))
    return ok("Trustee fetched successfully", TrusteeOut.model_validate(trustee))


@router.patch("/{trustee_id}", dependencies=[UPDATE])
async def update_trustee(
    trustee_id: UUID, body: TrusteeUpdate, current_user: CurrentUser, db: DBSession
):
    repo = TrusteeRepository(db)
    trustee = await repo.get_by_id(trustee_id)
    if trustee is None:
        raise NotFoundException("Trustee", str(trustee_id))
    await assert_group_access(db, current_user, trustee.group_id)
    if body.group_id is not None:
        await assert_group_access(db, current_user, body.group_id)
    trustee = await repo.update(trustee, **body.model_dump(exclude_unset=True))
    return ok("Trustee updated successfully", TrusteeOut.model_validate(trustee))


@router.delete("/{trustee_id}", dependencies=[DELETE])
async def delete_trustee(
    trustee_id: UUID, current_user: CurrentUser, db: DBSession
):
    repo = TrusteeRepository(db)
    trustee = await repo.get_by_id(trustee_id)
    if trustee is None:
        raise NotFoundException("Trustee", str(trustee_id))
    await assert_group_access(db, current_user, trustee.group_id)
    await repo.delete(trustee)
    return ok("Trustee deleted successfully")
