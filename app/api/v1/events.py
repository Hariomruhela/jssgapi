from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.constants import AuditAction
from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import assert_group_access, resolve_managed_group
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.event import Event
from app.repositories.event_repository import EventRepository
from app.schemas.event import EventCreate, EventOut, EventUpdate
from app.services.audit_service import AuditService

router = APIRouter(prefix="/events", tags=["Events"])

CREATE = Depends(require_permission(Permission.EVENT_CREATE))
UPDATE = Depends(require_permission(Permission.EVENT_UPDATE))
DELETE = Depends(require_permission(Permission.EVENT_DELETE))


@router.get("")
async def list_events(
    db: DBSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    upcoming_only: bool = Query(default=False),
):
    repo = EventRepository(db)
    if upcoming_only:
        items = await repo.list_upcoming(limit=page_size)
        return ok(
            "Upcoming events fetched successfully",
            [EventOut.model_validate(e) for e in items],
        )
    stmt = select(Event)
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Event.starts_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Events fetched successfully",
        [EventOut.model_validate(e) for e in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("", dependencies=[CREATE])
async def create_event(body: EventCreate, current_user: CurrentUser, db: DBSession):
    data = body.model_dump()
    data["group_id"] = await resolve_managed_group(db, current_user, body.group_id)
    event = await EventRepository(db).create(**data)
    await AuditService(db).log(
        AuditAction.EVENT_CREATE,
        user_id=current_user.id,
        entity_type="event",
        entity_id=event.id,
    )
    await db.flush()
    return created("Event created successfully", EventOut.model_validate(event))


@router.get("/upcoming")
async def list_upcoming_events(db: DBSession, limit: int = Query(50, ge=1, le=100)):
    events = await EventRepository(db).list_upcoming(limit=limit)
    return ok(
        "Upcoming events fetched successfully",
        [EventOut.model_validate(e) for e in events],
    )


@router.get("/{event_id}")
async def get_event(event_id: UUID, db: DBSession):
    event = await EventRepository(db).get_by_id(event_id)
    if event is None:
        raise NotFoundException("Event", str(event_id))
    return ok("Event fetched successfully", EventOut.model_validate(event))


@router.patch("/{event_id}", dependencies=[UPDATE])
async def update_event(
    event_id: UUID, body: EventUpdate, current_user: CurrentUser, db: DBSession
):
    repo = EventRepository(db)
    event = await repo.get_by_id(event_id)
    if event is None:
        raise NotFoundException("Event", str(event_id))
    await assert_group_access(db, current_user, event.group_id)
    if body.group_id is not None:
        await assert_group_access(db, current_user, body.group_id)
    event = await repo.update(event, **body.model_dump(exclude_unset=True))
    await AuditService(db).log(
        AuditAction.EVENT_UPDATE,
        user_id=current_user.id,
        entity_type="event",
        entity_id=event.id,
    )
    await db.flush()
    return ok("Event updated successfully", EventOut.model_validate(event))


@router.delete("/{event_id}", dependencies=[DELETE])
async def delete_event(event_id: UUID, current_user: CurrentUser, db: DBSession):
    repo = EventRepository(db)
    event = await repo.get_by_id(event_id)
    if event is None:
        raise NotFoundException("Event", str(event_id))
    await assert_group_access(db, current_user, event.group_id)
    await repo.delete(event)
    return ok("Event deleted successfully")
