from __future__ import annotations

from sqlalchemy import select

from app.models.trustee import Trustee
from app.repositories.base_repository import BaseRepository


class TrusteeRepository(BaseRepository[Trustee]):
    model = Trustee

    async def list_by_group(self, group_id) -> list[Trustee]:
        result = await self.session.execute(
            select(Trustee)
            .where(
                Trustee.group_id == group_id,
                Trustee.is_deleted.is_(False),
                Trustee.is_current.is_(True),
            )
            .order_by(Trustee.sort_order)
        )
        return list(result.scalars().all())
