from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.constants import AuditAction, MemberStatus
from app.core.exceptions import BadRequestException, NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import assert_group_access, resolve_managed_group
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.member import Member
from app.repositories.member_repository import MemberRepository
from app.schemas.member import (
    MemberApproveRequest,
    MemberCreate,
    MemberOut,
    MemberUpdate,
)
from app.services.audit_service import AuditService

router = APIRouter(prefix="/members", tags=["Members"])

CREATE = Depends(require_permission(Permission.MEMBER_CREATE))
UPDATE = Depends(require_permission(Permission.MEMBER_UPDATE))
DELETE = Depends(require_permission(Permission.MEMBER_DELETE))
APPROVE = Depends(require_permission(Permission.MEMBER_APPROVE))


@router.get("")
async def list_members(
    db: DBSession,
    group_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    query: str | None = Query(default=None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = MemberRepository(db)
    if query:
        members = await repo.search(query)
        return ok(
            "Members fetched successfully",
            [MemberOut.model_validate(m) for m in members],
        )
    stmt = select(Member)
    stmt = repo.apply_filters(
        stmt,
        {
            k: v
            for k, v in {"group_id": group_id, "membership_status": status}.items()
            if v is not None
        },
    )
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Member.first_name).limit(page_size).offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Members fetched successfully",
        [MemberOut.model_validate(i) for i in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("", dependencies=[CREATE])
async def create_member(body: MemberCreate, current_user: CurrentUser, db: DBSession):
    repo = MemberRepository(db)
    if await repo.get_by_user_id(body.user_id):
        raise BadRequestException("A member profile already exists for this user")
    data = body.model_dump()
    data["group_id"] = await resolve_managed_group(db, current_user, body.group_id)
    member = await repo.create(**data)
    await AuditService(db).log(
        AuditAction.MEMBER_CREATE, entity_type="member", entity_id=member.id
    )
    await db.flush()
    return created("Member created successfully", MemberOut.model_validate(member))


@router.get("/{member_id}")
async def get_member(member_id: UUID, db: DBSession):
    member = await MemberRepository(db).get_by_id(member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    return ok("Member fetched successfully", MemberOut.model_validate(member))


@router.patch("/{member_id}", dependencies=[UPDATE])
async def update_member(
    member_id: UUID, body: MemberUpdate, current_user: CurrentUser, db: DBSession
):
    repo = MemberRepository(db)
    member = await repo.get_by_id(member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    await assert_group_access(db, current_user, member.group_id)
    if body.group_id is not None:
        await assert_group_access(db, current_user, body.group_id)
    member = await repo.update(member, **body.model_dump(exclude_unset=True))
    required_fields = ["first_name", "last_name", "contact_email"]
    member.is_profile_complete = all(getattr(member, f) for f in required_fields)
    await AuditService(db).log(
        AuditAction.MEMBER_UPDATE, entity_type="member", entity_id=member.id
    )
    await db.flush()
    return ok("Member updated successfully", MemberOut.model_validate(member))


@router.post("/{member_id}/approve")
async def approve_member(
    member_id: UUID,
    body: MemberApproveRequest,
    db: DBSession,
    current_user=APPROVE,
):
    repo = MemberRepository(db)
    member = await repo.get_by_id(member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    await assert_group_access(db, current_user, member.group_id)
    if body.status in (
        MemberStatus.APPROVED,
        MemberStatus.REJECTED,
        MemberStatus.BLOCKED,
    ):
        if body.status != MemberStatus.APPROVED and not body.rejection_reason:
            raise BadRequestException("A rejection reason is required")
        member.membership_status = body.status.value
        if body.status == MemberStatus.APPROVED:
            member.approved_at = datetime.now(UTC)
            member.joined_at = datetime.now(UTC)
            member.approved_by = current_user.id
            member.rejection_reason = None
        else:
            member.rejection_reason = body.rejection_reason
        action = (
            AuditAction.MEMBER_APPROVE
            if body.status == MemberStatus.APPROVED
            else AuditAction.MEMBER_REJECT
            if body.status == MemberStatus.REJECTED
            else AuditAction.MEMBER_BLOCK
        )
        await AuditService(db).log(
            action, user_id=current_user.id, entity_type="member", entity_id=member.id
        )
        await db.flush()
        await db.refresh(member)
    else:
        raise BadRequestException("Use update endpoint for status changes")
    return ok("Member status updated successfully", MemberOut.model_validate(member))


@router.delete("/{member_id}", dependencies=[DELETE])
async def delete_member(member_id: UUID, current_user: CurrentUser, db: DBSession):
    repo = MemberRepository(db)
    member = await repo.get_by_id(member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    await assert_group_access(db, current_user, member.group_id)
    await repo.delete(member)
    return ok("Member deleted successfully")
