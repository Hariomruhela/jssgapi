from __future__ import annotations

from sqlalchemy import or_, select

from app.models.group import SocialGroup
from app.repositories.base_repository import BaseRepository


class GroupRepository(BaseRepository[SocialGroup]):
    model = SocialGroup

    async def search(self, query: str, limit: int = 50) -> list[SocialGroup]:
        term = f"%{query}%"
        result = await self.session.execute(
            select(SocialGroup)
            .where(
                SocialGroup.is_deleted.is_(False),
                or_(SocialGroup.name.ilike(term), SocialGroup.name_hi.ilike(term)),
            )
            .order_by(SocialGroup.name)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_by_name(self, name: str) -> SocialGroup | None:
        result = await self.session.execute(
            select(SocialGroup).where(SocialGroup.name == name)
        )
        return result.scalar_one_or_none()
