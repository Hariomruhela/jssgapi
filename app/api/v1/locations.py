from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.dependencies import DBSession, require_permission
from app.models.location import Location
from app.repositories.location_repository import LocationRepository
from app.schemas.location import LocationCreate, LocationOut, LocationUpdate

router = APIRouter(prefix="/locations", tags=["Locations"])

CREATE = Depends(require_permission(Permission.LOCATION_CREATE))
UPDATE = Depends(require_permission(Permission.LOCATION_UPDATE))
DELETE = Depends(require_permission(Permission.LOCATION_DELETE))


@router.get("")
async def list_locations(
    db: DBSession,
    level: str | None = Query(default=None),
    parent_id: UUID | None = Query(default=None),
    city: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = LocationRepository(db)
    stmt = select(Location)
    stmt = repo.apply_filters(
        stmt,
        {
            k: v
            for k, v in {"level": level, "parent_id": parent_id, "city": city}.items()
            if v is not None
        },
    )
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Location.name).limit(page_size).offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Locations fetched successfully",
        [LocationOut.model_validate(i) for i in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.get("/tree")
async def location_tree(db: DBSession, root: UUID | None = Query(default=None)):
    result = await db.execute(select(Location).where(Location.is_deleted.is_(False)))
    locations = list(result.scalars().all())
    by_parent: dict[UUID | None, list[dict]] = {}
    for loc in locations:
        by_parent.setdefault(loc.parent_id, []).append(
            {"id": loc.id, "name": loc.name, "level": loc.level, "children": []}
        )
    node_ids = {loc.id for loc in locations}
    for _parent_id, children in by_parent.items():
        for child in children:
            child["children"] = by_parent.get(child["id"], [])
    roots = by_parent.get(root)
    if roots is None:
        roots = []
        for loc in locations:
            if loc.parent_id is None or loc.parent_id not in node_ids:
                roots.append(by_parent[loc.id][0])
    return ok("Location tree fetched successfully", roots)


@router.post("", dependencies=[CREATE])
async def create_location(body: LocationCreate, db: DBSession):
    loc = await LocationRepository(db).create(**body.model_dump())
    await db.flush()
    return created("Location created successfully", LocationOut.model_validate(loc))


@router.get("/{location_id}")
async def get_location(location_id: UUID, db: DBSession):
    loc = await LocationRepository(db).get_by_id(location_id)
    if loc is None:
        raise NotFoundException("Location", str(location_id))
    return ok("Location fetched successfully", LocationOut.model_validate(loc))


@router.patch("/{location_id}", dependencies=[UPDATE])
async def update_location(location_id: UUID, body: LocationUpdate, db: DBSession):
    repo = LocationRepository(db)
    loc = await repo.get_by_id(location_id)
    if loc is None:
        raise NotFoundException("Location", str(location_id))
    loc = await repo.update(loc, **body.model_dump(exclude_unset=True))
    return ok("Location updated successfully", LocationOut.model_validate(loc))


@router.delete("/{location_id}", dependencies=[DELETE])
async def delete_location(location_id: UUID, db: DBSession):
    repo = LocationRepository(db)
    loc = await repo.get_by_id(location_id)
    if loc is None:
        raise NotFoundException("Location", str(location_id))
    await repo.delete(loc)
    return ok("Location deleted successfully")
