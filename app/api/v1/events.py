from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.constants import AuditAction, EventStatus
from app.core.exceptions import ForbiddenException, NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import (
    AccessScope,
    assert_group_access,
    get_access_scope,
    resolve_managed_group,
)
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.event import Event
from app.repositories.event_repository import EventRepository
from app.schemas.event import EventCreate, EventOut, EventUpdate
from app.services.audit_service import AuditService

router = APIRouter(prefix="/events", tags=["Events"])

READ = Depends(require_permission(Permission.EVENT_READ))
CREATE = Depends(require_permission(Permission.EVENT_CREATE))
UPDATE = Depends(require_permission(Permission.EVENT_UPDATE))
DELETE = Depends(require_permission(Permission.EVENT_DELETE))


@router.get("", dependencies=[READ])
async def list_events(
    db: DBSession,
    current_user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    upcoming_only: bool = Query(default=False),
):
    repo = EventRepository(db)
    scope = await get_access_scope(db, current_user)
    if group_id is not None and not scope.unrestricted and group_id != scope.group_id:
        raise ForbiddenException("Operation is limited to your own group")

    stmt = repo.apply_filters(select(Event), _event_filters(db, scope, group_id))
    if upcoming_only:
        stmt = stmt.where(
            Event.starts_at >= datetime.now(UTC),
            Event.status != EventStatus.CANCELLED.value,
        )
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


def _event_filters(
    db: DBSession, scope: AccessScope, group_id: UUID | None
) -> dict[str, object]:
    """Group filter for a listing.

    Forced for a scoped role, optional for SUPER_ADMIN.
    """
    if scope.unrestricted:
        return {"group_id": group_id} if group_id is not None else {}
    return {"group_id": scope.group_id}


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


@router.get("/upcoming", dependencies=[READ])
async def list_upcoming_events(
    db: DBSession, current_user: CurrentUser, limit: int = Query(50, ge=1, le=100)
):
    scope = await get_access_scope(db, current_user)
    stmt = (
        select(Event)
        .where(
            Event.starts_at >= datetime.now(UTC),
            Event.status != EventStatus.CANCELLED.value,
            *[_event_filter_clause(scope)],
        )
        .order_by(Event.starts_at)
        .limit(limit)
    )
    result = await db.execute(stmt)
    events = list(result.scalars().all())
    return ok(
        "Upcoming events fetched successfully",
        [EventOut.model_validate(e) for e in events],
    )


def _event_filter_clause(scope: AccessScope):
    return Event.group_id == scope.group_id if not scope.unrestricted else True


@router.get("/{event_id}", dependencies=[READ])
async def get_event(event_id: UUID, db: DBSession, current_user: CurrentUser):
    event = await EventRepository(db).get_by_id(event_id)
    if event is None:
        raise NotFoundException("Event", str(event_id))
    scope = await get_access_scope(db, current_user)
    # NULL group_id is a platform-wide event and is not visible to a group admin.
    if not scope.unrestricted and event.group_id != scope.group_id:
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
