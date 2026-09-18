from __future__ import annotations

from sqlalchemy import or_, select

from app.models.member import Member
from app.repositories.base_repository import BaseRepository


class MemberRepository(BaseRepository[Member]):
    model = Member

    async def get_by_user_id(self, user_id) -> Member | None:
        result = await self.session.execute(
            select(Member).where(Member.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_membership_number(self, number: str) -> Member | None:
        result = await self.session.execute(
            select(Member).where(Member.membership_number == number)
        )
        return result.scalar_one_or_none()

    async def list_by_group(
        self, group_id, limit: int = 100, offset: int = 0
    ) -> list[Member]:
        result = await self.session.execute(
            select(Member)
            .where(Member.group_id == group_id, Member.is_deleted.is_(False))
            .order_by(Member.first_name)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def search(self, query: str, limit: int = 50) -> list[Member]:
        term = f"%{query}%"
        result = await self.session.execute(
            select(Member)
            .where(
                Member.is_deleted.is_(False),
                or_(
                    Member.first_name.ilike(term),
                    Member.last_name.ilike(term),
                    Member.contact_email.ilike(term),
                    Member.contact_phone.ilike(term),
                    Member.membership_number.ilike(term),
                ),
            )
            .order_by(Member.first_name)
            .limit(limit)
        )
        return list(result.scalars().all())
