from __future__ import annotations

from sqlalchemy import or_, select

from app.core.constants import NewsStatus
from app.models.news import News
from app.repositories.base_repository import BaseRepository


class NewsRepository(BaseRepository[News]):
    model = News

    async def list_published(self, limit: int, offset: int) -> list[News]:
        result = await self.session.execute(
            select(News)
            .where(
                News.is_deleted.is_(False), News.status == NewsStatus.PUBLISHED.value
            )
            .order_by(News.published_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def search(self, query: str, limit: int = 50) -> list[News]:
        term = f"%{query}%"
        result = await self.session.execute(
            select(News)
            .where(
                News.is_deleted.is_(False),
                or_(News.title.ilike(term), News.title_hi.ilike(term)),
            )
            .order_by(News.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
