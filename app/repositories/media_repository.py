from __future__ import annotations

from sqlalchemy import func, select

from app.models.media import Media
from app.repositories.base_repository import BaseRepository


class MediaRepository(BaseRepository[Media]):
    model = Media

    async def list_for_owner(
        self, owner_type: str, owner_id, limit: int, offset: int
    ) -> list[Media]:
        result = await self.session.execute(
            select(Media)
            .where(*_owner_clauses(owner_type, owner_id))
            .order_by(Media.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_owner(self, owner_type: str, owner_id) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(Media)
            .where(*_owner_clauses(owner_type, owner_id))
        )
        return result.scalar_one()

    async def list_for_group(self, group_id, limit: int, offset: int) -> list[Media]:
        result = await self.session.execute(
            select(Media)
            .where(*_group_clauses(group_id))
            .order_by(Media.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_group(self, group_id) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Media).where(*_group_clauses(group_id))
        )
        return result.scalar_one()

    async def get_by_object_key(self, object_key: str) -> Media | None:
        result = await self.session.execute(
            select(Media).where(Media.r2_object_key == object_key)
        )
        return result.scalar_one_or_none()


def _owner_clauses(owner_type: str, owner_id) -> tuple:
    return (
        Media.owner_type == owner_type,
        Media.owner_id == owner_id,
        Media.is_deleted.is_(False),
    )


def _group_clauses(group_id) -> tuple:
    """Filter by group.

    ``group_id=None`` means "no filter" for an unrestricted caller listing
    everything. A scoped caller never reaches this with None: ``get_access_scope``
    raises first when it has no group.
    """
    clauses = [Media.is_deleted.is_(False)]
    if group_id is not None:
        clauses.append(Media.group_id == group_id)
    return tuple(clauses)
