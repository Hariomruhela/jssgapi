from __future__ import annotations

from sqlalchemy import select

from app.core.constants import AdvertisementStatus
from app.models.advertisement import Advertisement
from app.repositories.base_repository import BaseRepository


class AdvertisementRepository(BaseRepository[Advertisement]):
    model = Advertisement

    async def list_active(self, limit: int = 50) -> list[Advertisement]:
        result = await self.session.execute(
            select(Advertisement)
            .where(
                Advertisement.is_deleted.is_(False),
                Advertisement.is_active.is_(True),
                Advertisement.status == AdvertisementStatus.APPROVED.value,
            )
            .order_by(Advertisement.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_by_placement(self, placement: str) -> list[Advertisement]:
        result = await self.session.execute(
            select(Advertisement)
            .where(
                Advertisement.is_deleted.is_(False),
                Advertisement.is_active.is_(True),
                Advertisement.placement == placement,
                Advertisement.status == AdvertisementStatus.APPROVED.value,
            )
            .order_by(Advertisement.created_at.desc())
        )
        return list(result.scalars().all())
