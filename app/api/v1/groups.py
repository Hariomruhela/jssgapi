from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.constants import AuditAction
from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import assert_group_access
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.group import SocialGroup
from app.repositories.group_repository import GroupRepository
from app.schemas.group import SocialGroupCreate, SocialGroupOut, SocialGroupUpdate
from app.services.audit_service import AuditService

router = APIRouter(prefix="/groups", tags=["Groups"])

CREATE = Depends(require_permission(Permission.GROUP_CREATE))
UPDATE = Depends(require_permission(Permission.GROUP_UPDATE))
DELETE = Depends(require_permission(Permission.GROUP_DELETE))


@router.get("")
async def list_groups(
    db: DBSession,
    query: str | None = Query(default=None, max_length=200),
    status: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = GroupRepository(db)
    if query:
        groups = await repo.search(query)
        return ok(
            "Groups fetched successfully",
            [SocialGroupOut.model_validate(g) for g in groups],
        )
    stmt = select(SocialGroup)
    stmt = repo.apply_filters(
        stmt, {k: v for k, v in {"status": status}.items() if v is not None}
    )
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(SocialGroup.name).limit(page_size).offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Groups fetched successfully",
        [SocialGroupOut.model_validate(i) for i in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("", dependencies=[CREATE])
async def create_group(body: SocialGroupCreate, db: DBSession):
    repo = GroupRepository(db)
    existing = await repo.get_by_name(body.name)
    if existing:
        return created(
            "Group created successfully", SocialGroupOut.model_validate(existing)
        )
    group = await repo.create(**body.model_dump())
    await db.flush()
    return created("Group created successfully", SocialGroupOut.model_validate(group))


@router.get("/{group_id}")
async def get_group(group_id: UUID, db: DBSession):
    group = await GroupRepository(db).get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    return ok("Group fetched successfully", SocialGroupOut.model_validate(group))


@router.patch("/{group_id}", dependencies=[UPDATE])
async def update_group(
    group_id: UUID, body: SocialGroupUpdate, current_user: CurrentUser, db: DBSession
):
    repo = GroupRepository(db)
    group = await repo.get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    await assert_group_access(db, current_user, group.id)
    group = await repo.update(group, **body.model_dump(exclude_unset=True))
    await AuditService(db).log(
        AuditAction.GROUP_UPDATE, entity_type="group", entity_id=group.id
    )
    await db.flush()
    return ok("Group updated successfully", SocialGroupOut.model_validate(group))


@router.delete("/{group_id}", dependencies=[DELETE])
async def delete_group(group_id: UUID, current_user: CurrentUser, db: DBSession):
    repo = GroupRepository(db)
    group = await repo.get_by_id(group_id)
    if group is None:
        raise NotFoundException("Group", str(group_id))
    await assert_group_access(db, current_user, group.id)
    await repo.delete(group)
    await AuditService(db).log(
        AuditAction.GROUP_DELETE, entity_type="group", entity_id=group.id
    )
    await db.flush()
    return ok("Group deleted successfully")
