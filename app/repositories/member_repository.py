from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

from app.models.group import SocialGroup
from app.models.member import Member
from app.repositories.base_repository import BaseRepository

# ``Member.group`` and ``Member.location`` use the default lazy strategy, which
# cannot be resolved inside an async session. Any query whose caller needs the
# profile fields (social_group_name, city, area) must eager load them.
MEMBER_PROFILE_LOADS = (
    selectinload(Member.group),
    selectinload(Member.location),
)


def group_name_filter(group_name: str):
    """Case-insensitive match of a member's social group by name.

    A member's group name lives in one of two places: the free-text
    ``social_group_name`` kept in ``members.profile_data`` (sheet imports and
    members never linked to a group), or the linked ``social_groups`` row when
    ``group_id`` is set. Either may match, so the filter can never hide a member
    whose name is stored on the other side.
    """
    needle = group_name.strip().casefold()
    # Stored names may carry stray padding (sheet imports and free-text edits do
    # not trim), while the filter value comes from the trimmed option list, so
    # both sides are trimmed before the case-insensitive comparison.
    profile_name = func.lower(
        func.trim(Member.profile_data["social_group_name"].astext)
    )
    linked_group = (
        select(SocialGroup.id)
        .where(
            SocialGroup.id == Member.group_id,
            SocialGroup.is_deleted.is_(False),
            func.lower(func.trim(SocialGroup.name)) == needle,
        )
        .exists()
    )
    return or_(profile_name == needle, and_(Member.group_id.is_not(None), linked_group))


class MemberRepository(BaseRepository[Member]):
    model = Member

    async def get_by_id(self, instance_id) -> Member | None:
        """Fetch a member that is not soft-deleted.

        ``DELETE /api/v1/members/{id}`` only flips ``is_deleted``, so a lookup
        that ignores the flag would keep serving a member that was deleted,
        and every write endpoint would still accept it.
        """
        result = await self.session.execute(
            select(Member).where(Member.id == instance_id, Member.is_deleted.is_(False))
        )
        return result.scalar_one_or_none()

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

    async def search(
        self,
        query: str,
        group_id=None,
        membership_status: str | None = None,
        group_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Member]:
        term = f"%{query}%"
        stmt = (
            select(Member)
            .options(*MEMBER_PROFILE_LOADS)
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
        )
        # The caller-supplied filters must apply to a search too. Returning every
        # match regardless of group is how a search would leak across groups.
        if group_id is not None:
            stmt = stmt.where(Member.group_id == group_id)
        if membership_status is not None:
            stmt = stmt.where(Member.membership_status == membership_status)
        if group_name:
            stmt = stmt.where(group_name_filter(group_name))
        result = await self.session.execute(
            stmt.order_by(Member.first_name).limit(limit).offset(offset)
        )
        return list(result.scalars().all())
