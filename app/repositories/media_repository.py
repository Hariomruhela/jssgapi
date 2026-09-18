from __future__ import annotations

from sqlalchemy import select

from app.models.media import Media
from app.repositories.base_repository import BaseRepository


class MediaRepository(BaseRepository[Media]):
    model = Media

    async def list_for_owner(
        self, owner_type: str, owner_id, limit: int, offset: int
    ) -> list[Media]:
        result = await self.session.execute(
            select(Media)
            .where(
                Media.owner_type == owner_type,
                Media.owner_id == owner_id,
                Media.is_deleted.is_(False),
            )
            .order_by(Media.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def get_by_object_key(self, object_key: str) -> Media | None:
        result = await self.session.execute(
            select(Media).where(Media.r2_object_key == object_key)
        )
        return result.scalar_one_or_none()
