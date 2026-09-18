from __future__ import annotations

from sqlalchemy import select

from app.models.location import Location
from app.repositories.base_repository import BaseRepository


class LocationRepository(BaseRepository[Location]):
    model = Location

    async def list_level(self, level: str) -> list[Location]:
        result = await self.session.execute(
            select(Location)
            .where(Location.level == level, Location.is_deleted.is_(False))
            .order_by(Location.name)
        )
        return list(result.scalars().all())

    async def list_children(self, parent_id) -> list[Location]:
        result = await self.session.execute(
            select(Location)
            .where(Location.parent_id == parent_id, Location.is_deleted.is_(False))
            .order_by(Location.name)
        )
        return list(result.scalars().all())
