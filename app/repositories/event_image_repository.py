from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models.event_image import EventImage
from app.repositories.base_repository import BaseRepository


class EventImageRepository(BaseRepository[EventImage]):
    model = EventImage

    async def list_for_event(
        self, event_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list[EventImage]:
        result = await self.session.execute(
            select(EventImage)
            .where(
                EventImage.event_id == event_id,
                EventImage.is_deleted.is_(False),
            )
            .order_by(EventImage.position.asc(), EventImage.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_event(self, event_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(EventImage)
            .where(
                EventImage.event_id == event_id,
                EventImage.is_deleted.is_(False),
            )
        )
        return int(result.scalar_one())

    async def batch_count_for_events(
        self, event_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        if not event_ids:
            return {}
        result = await self.session.execute(
            select(EventImage.event_id, func.count())
            .where(
                EventImage.event_id.in_(event_ids),
                EventImage.is_deleted.is_(False),
            )
            .group_by(EventImage.event_id)
        )
        counts = dict(result.all())
        return {eid: counts.get(eid, 0) for eid in event_ids}

    async def get_by_id(self, instance_id: uuid.UUID) -> EventImage | None:
        result = await self.session.execute(
            select(EventImage).where(
                EventImage.id == instance_id,
                EventImage.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def next_position(self, event_id: uuid.UUID) -> int:
        """The position a newly added image should take."""
        count = await self.count_for_event(event_id)
        return count
