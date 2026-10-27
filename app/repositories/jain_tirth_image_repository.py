from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models.jain_tirth_image import JainTirthImage
from app.repositories.base_repository import BaseRepository


class JainTirthImageRepository(BaseRepository[JainTirthImage]):
    model = JainTirthImage

    async def list_for_tirth(
        self, jain_tirth_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list[JainTirthImage]:
        result = await self.session.execute(
            select(JainTirthImage)
            .where(
                JainTirthImage.jain_tirth_id == jain_tirth_id,
                JainTirthImage.is_deleted.is_(False),
            )
            .order_by(
                JainTirthImage.position.asc(), JainTirthImage.created_at.asc()
            )
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_tirth(self, jain_tirth_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(JainTirthImage)
            .where(
                JainTirthImage.jain_tirth_id == jain_tirth_id,
                JainTirthImage.is_deleted.is_(False),
            )
        )
        return int(result.scalar_one())

    async def batch_count_for_tirths(
        self, tirth_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        if not tirth_ids:
            return {}
        result = await self.session.execute(
            select(JainTirthImage.jain_tirth_id, func.count())
            .where(
                JainTirthImage.jain_tirth_id.in_(tirth_ids),
                JainTirthImage.is_deleted.is_(False),
            )
            .group_by(JainTirthImage.jain_tirth_id)
        )
        counts = dict(result.all())
        return {tid: counts.get(tid, 0) for tid in tirth_ids}

    async def get_by_id(self, instance_id: uuid.UUID) -> JainTirthImage | None:
        result = await self.session.execute(
            select(JainTirthImage).where(
                JainTirthImage.id == instance_id,
                JainTirthImage.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()
