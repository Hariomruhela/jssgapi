from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok
from app.dependencies import DBSession, require_permission
from app.models.family import FamilyMember
from app.models.member import Member
from app.repositories.family_repository import FamilyMemberRepository
from app.schemas.family import FamilyMemberCreate, FamilyMemberOut, FamilyMemberUpdate

router = APIRouter(prefix="/members/{member_id}/family", tags=["Family"])

CREATE = Depends(require_permission(Permission.MEMBER_UPDATE))
UPDATE = Depends(require_permission(Permission.MEMBER_UPDATE))
DELETE = Depends(require_permission(Permission.MEMBER_UPDATE))


async def _get_member(db, member_id: UUID) -> Member:
    result = await db.execute(select(Member).where(Member.id == member_id))
    member = result.scalar_one_or_none()
    if member is None:
        raise NotFoundException("Member", str(member_id))
    return member


@router.get("")
async def list_family(member_id: UUID, db: DBSession):
    await _get_member(db, member_id)
    result = await db.execute(
        select(FamilyMember)
        .where(FamilyMember.member_id == member_id)
        .order_by(FamilyMember.created_at)
    )
    items = [FamilyMemberOut.model_validate(f) for f in result.scalars().all()]
    return ok("Family members fetched successfully", items)


@router.post("", dependencies=[CREATE])
async def create_family_member(
    member_id: UUID, body: FamilyMemberCreate, db: DBSession
):
    await _get_member(db, member_id)
    member = await FamilyMemberRepository(db).create(
        member_id=member_id, **body.model_dump()
    )
    await db.flush()
    return created(
        "Family member created successfully", FamilyMemberOut.model_validate(member)
    )


@router.get("/{family_id}")
async def get_family_member(member_id: UUID, family_id: UUID, db: DBSession):
    await _get_member(db, member_id)
    result = await db.execute(
        select(FamilyMember).where(
            FamilyMember.id == family_id, FamilyMember.member_id == member_id
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise NotFoundException("Family member", str(family_id))
    return ok(
        "Family member fetched successfully", FamilyMemberOut.model_validate(item)
    )


@router.patch("/{family_id}", dependencies=[UPDATE])
async def update_family_member(
    member_id: UUID, family_id: UUID, body: FamilyMemberUpdate, db: DBSession
):
    repo = FamilyMemberRepository(db)
    result = await db.execute(
        select(FamilyMember).where(
            FamilyMember.id == family_id, FamilyMember.member_id == member_id
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise NotFoundException("Family member", str(family_id))
    item = await repo.update(item, **body.model_dump(exclude_unset=True))
    return ok(
        "Family member updated successfully", FamilyMemberOut.model_validate(item)
    )


@router.delete("/{family_id}", dependencies=[DELETE])
async def delete_family_member(member_id: UUID, family_id: UUID, db: DBSession):
    repo = FamilyMemberRepository(db)
    result = await db.execute(
        select(FamilyMember).where(
            FamilyMember.id == family_id, FamilyMember.member_id == member_id
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise NotFoundException("Family member", str(family_id))
    await repo.delete(item)
    return ok("Family member deleted successfully")
