from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok
from app.dependencies import DBSession, require_permission
from app.models.member import Member
from app.models.professional import ProfessionalInformation
from app.repositories.professional_repository import ProfessionalInfoRepository
from app.schemas.professional import (
    ProfessionalInfoCreate,
    ProfessionalInfoOut,
    ProfessionalInfoUpdate,
)

router = APIRouter(prefix="/members/{member_id}/professional", tags=["Professional"])

WRITE = Depends(require_permission(Permission.MEMBER_UPDATE))


async def _get_member(db, member_id: UUID) -> Member:
    result = await db.execute(select(Member).where(Member.id == member_id))
    member = result.scalar_one_or_none()
    if member is None:
        raise NotFoundException("Member", str(member_id))
    return member


@router.get("")
async def list_professional(member_id: UUID, db: DBSession):
    await _get_member(db, member_id)
    result = await db.execute(
        select(ProfessionalInformation)
        .where(ProfessionalInformation.member_id == member_id)
        .order_by(ProfessionalInformation.created_at)
    )
    items = [ProfessionalInfoOut.model_validate(p) for p in result.scalars().all()]
    return ok("Professional information fetched successfully", items)


@router.post("", dependencies=[WRITE])
async def create_professional_info(
    member_id: UUID, body: ProfessionalInfoCreate, db: DBSession
):
    await _get_member(db, member_id)
    info = await ProfessionalInfoRepository(db).create(
        member_id=member_id, **body.model_dump()
    )
    await db.flush()
    return created(
        "Professional information created successfully",
        ProfessionalInfoOut.model_validate(info),
    )


@router.get("/{info_id}")
async def get_professional_info(member_id: UUID, info_id: UUID, db: DBSession):
    await _get_member(db, member_id)
    result = await db.execute(
        select(ProfessionalInformation).where(
            ProfessionalInformation.id == info_id,
            ProfessionalInformation.member_id == member_id,
        )
    )
    info = result.scalar_one_or_none()
    if info is None:
        raise NotFoundException("Professional information", str(info_id))
    return ok(
        "Professional information fetched successfully",
        ProfessionalInfoOut.model_validate(info),
    )


@router.patch("/{info_id}", dependencies=[WRITE])
async def update_professional_info(
    member_id: UUID,
    info_id: UUID,
    body: ProfessionalInfoUpdate,
    db: DBSession,
):
    repo = ProfessionalInfoRepository(db)
    result = await db.execute(
        select(ProfessionalInformation).where(
            ProfessionalInformation.id == info_id,
            ProfessionalInformation.member_id == member_id,
        )
    )
    info = result.scalar_one_or_none()
    if info is None:
        raise NotFoundException("Professional information", str(info_id))
    info = await repo.update(info, **body.model_dump(exclude_unset=True))
    return ok(
        "Professional information updated successfully",
        ProfessionalInfoOut.model_validate(info),
    )


@router.delete("/{info_id}", dependencies=[WRITE])
async def delete_professional_info(member_id: UUID, info_id: UUID, db: DBSession):
    repo = ProfessionalInfoRepository(db)
    result = await db.execute(
        select(ProfessionalInformation).where(
            ProfessionalInformation.id == info_id,
            ProfessionalInformation.member_id == member_id,
        )
    )
    info = result.scalar_one_or_none()
    if info is None:
        raise NotFoundException("Professional information", str(info_id))
    await repo.delete(info)
    return ok("Professional information deleted successfully")
