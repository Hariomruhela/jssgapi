from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select

from app.core.constants import EventStatus
from app.models.event import Event
from app.repositories.base_repository import BaseRepository


class EventRepository(BaseRepository[Event]):
    model = Event

    async def list_public(self, limit: int, offset: int) -> list[Event]:
        result = await self.session.execute(
            select(Event)
            .where(
                Event.is_deleted.is_(False),
                Event.is_public.is_(True),
                Event.status.in_(
                    [EventStatus.PUBLISHED.value, EventStatus.COMPLETED.value]
                ),
            )
            .order_by(Event.starts_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def list_upcoming(self, limit: int = 50) -> list[Event]:
        result = await self.session.execute(
            select(Event)
            .where(
                Event.is_deleted.is_(False),
                Event.is_public.is_(True),
                Event.status == EventStatus.PUBLISHED.value,
                Event.starts_at >= datetime.now(UTC),
            )
            .order_by(Event.starts_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def search(self, query: str, limit: int = 50) -> list[Event]:
        term = f"%{query}%"
        result = await self.session.execute(
            select(Event)
            .where(
                Event.is_deleted.is_(False),
                or_(Event.title.ilike(term), Event.title_hi.ilike(term)),
            )
            .order_by(Event.starts_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
