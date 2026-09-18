from __future__ import annotations

from sqlalchemy import select

from app.models.fee import Fee
from app.repositories.base_repository import BaseRepository


class FeeRepository(BaseRepository[Fee]):
    model = Fee

    async def list_by_financial_year(self, financial_year: str) -> list[Fee]:
        result = await self.session.execute(
            select(Fee)
            .where(Fee.is_deleted.is_(False), Fee.financial_year == financial_year)
            .order_by(Fee.due_date)
        )
        return list(result.scalars().all())

    async def list_by_group(self, group_id) -> list[Fee]:
        result = await self.session.execute(
            select(Fee)
            .where(Fee.is_deleted.is_(False), Fee.group_id == group_id)
            .order_by(Fee.due_date)
        )
        return list(result.scalars().all())
