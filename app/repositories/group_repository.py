from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select

from app.models.group import SocialGroup
from app.repositories.base_repository import BaseRepository


class GroupRepository(BaseRepository[SocialGroup]):
    model = SocialGroup

    async def get_by_id(self, instance_id: uuid.UUID) -> SocialGroup | None:
        """A soft-deleted group reads as missing, so it cannot be fetched,
        edited or deleted a second time."""
        result = await self.session.execute(
            select(SocialGroup).where(
                SocialGroup.id == instance_id,
                SocialGroup.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    async def search(
        self,
        query: str,
        group_id=None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SocialGroup]:
        term = f"%{query}%"
        stmt = (
            select(SocialGroup)
            .where(
                SocialGroup.is_deleted.is_(False),
                or_(SocialGroup.name.ilike(term), SocialGroup.name_hi.ilike(term)),
            )
            .order_by(SocialGroup.name)
            .limit(limit)
            .offset(offset)
        )
        # Scope must narrow a search as well as a listing, otherwise searching by
        # name leaks groups the caller is not allowed to see.
        if group_id is not None:
            stmt = stmt.where(SocialGroup.id == group_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_name(self, name: str) -> SocialGroup | None:
        # A soft-deleted group must not be returned: otherwise re-creating a
        # deleted group's name would hand back the deleted row, which is then
        # invisible in the listing. The name is free again after a delete.
        #
        # The match ignores case and surrounding padding because names arrive
        # from free-text intake (sheets, manual entry), so the same group can be
        # re-submitted as "Jssg professional unity" after "JSSG Professional
        # Unity" already exists. ``POST /groups`` then returns the existing row
        # instead of creating a second one.
        needle = name.strip().lower()
        result = await self.session.execute(
            select(SocialGroup)
            .where(
                func.lower(func.trim(SocialGroup.name)) == needle,
                SocialGroup.is_deleted.is_(False),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()
