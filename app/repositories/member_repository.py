from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

from app.models.group import SocialGroup
from app.models.location import Location
from app.models.member import Member
from app.models.professional import ProfessionalInformation
from app.repositories.base_repository import BaseRepository
from app.utils.helpers import is_valid_uuid

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


#: The characters trimmed off a stored designation. ``btrim`` only strips
#: spaces by default, so the whitespace a sheet import may leave behind is
#: listed explicitly.
_TRIM_CHARS = " \t\n\r"


def designation_filter(designation: str | None = None):
    """Members that carry a group designation, optionally a specific one.

    A trustee is a community member whose group designation is filled in, so
    this filter is what separates the trustee listing from the member listing.
    The designation is kept in ``members.profile_data`` and mirrored into
    ``professional_information.designation``; either store may hold it, so both
    are read. A blank value (``null``, ``""`` or whitespace only) means the
    member has no designation and is therefore not a trustee.
    """
    stored = func.btrim(Member.profile_data["group_designation"].astext, _TRIM_CHARS)
    mirrored = (
        select(ProfessionalInformation.member_id)
        .where(
            ProfessionalInformation.member_id == Member.id,
            func.btrim(ProfessionalInformation.designation, _TRIM_CHARS) != "",
        )
        .exists()
    )
    condition = or_(stored != "", mirrored)
    needle = (designation or "").strip()
    if not needle:
        return condition
    # The designation is free text, so the match is case-insensitive and the
    # padded stored value is trimmed on both sides before comparing.
    return and_(
        condition,
        or_(
            func.lower(stored) == needle.casefold(),
            Member.id.in_(
                select(ProfessionalInformation.member_id).where(
                    func.lower(
                        func.btrim(ProfessionalInformation.designation, _TRIM_CHARS)
                    )
                    == needle.casefold()
                )
            ),
        ),
    )


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
        designation: str | None = None,
        trustee_only: bool = False,
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
        if trustee_only or (designation or "").strip():
            stmt = stmt.where(
                designation_filter(designation if (designation or "").strip() else None)
            )
        result = await self.session.execute(
            stmt.order_by(Member.first_name).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def search_detailed(
        self,
        query: str,
        group_id=None,
        membership_status: str | None = None,
        group_name: str | None = None,
        location: str | None = None,
        member_id=None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Member], int]:
        """Keyword search across every member field, plus the total count.

        Matches the member's own columns, every profile field kept in
        ``profile_data``, the linked group's name and the linked location. A
        UUID keyword also matches the row id, so an id pasted into the search
        box resolves the member directly. The caller-supplied filters narrow the
        result exactly as they do a listing - a search must never widen scope.
        """
        term = f"%{query}%"
        location_needle = f"%{location}%" if location else None
        conditions = [
            Member.first_name.ilike(term),
            Member.middle_name.ilike(term),
            Member.last_name.ilike(term),
            Member.contact_email.ilike(term),
            Member.contact_phone.ilike(term),
            Member.address_line.ilike(term),
            Member.occupation_summary.ilike(term),
            Member.membership_number.ilike(term),
            Member.blood_group.ilike(term),
            Member.gender.ilike(term),
            Member.membership_status.ilike(term),
            Member.profile_data["full_name"].astext.ilike(term),
            Member.profile_data["city"].astext.ilike(term),
            Member.profile_data["area"].astext.ilike(term),
            Member.profile_data["address"].astext.ilike(term),
            Member.profile_data["social_group_name"].astext.ilike(term),
            Member.profile_data["member_occupation"].astext.ilike(term),
            Member.profile_data["member_education"].astext.ilike(term),
            Member.profile_data["company_name"].astext.ilike(term),
            Member.group_id.in_(
                select(SocialGroup.id).where(
                    SocialGroup.is_deleted.is_(False),
                    SocialGroup.name.ilike(term),
                )
            ),
            Member.location_id.in_(
                select(Location.id).where(
                    or_(
                        Location.name.ilike(term),
                        Location.city.ilike(term),
                        Location.area.ilike(term),
                    )
                )
            ),
        ]
        if is_valid_uuid(query):
            conditions.append(Member.id == UUID(query))

        stmt = (
            select(Member)
            .options(*MEMBER_PROFILE_LOADS)
            .where(Member.is_deleted.is_(False), or_(*conditions))
        )
        if group_id is not None:
            stmt = stmt.where(Member.group_id == group_id)
        if member_id is not None:
            stmt = stmt.where(Member.id == member_id)
        if membership_status is not None:
            stmt = stmt.where(Member.membership_status == membership_status)
        if group_name:
            stmt = stmt.where(group_name_filter(group_name))
        if location_needle is not None:
            stmt = stmt.where(
                Member.location_id.in_(
                    select(Location.id).where(
                        or_(
                            Location.name.ilike(location_needle),
                            Location.city.ilike(location_needle),
                            Location.area.ilike(location_needle),
                        )
                    )
                )
            )

        total = (
            await self.session.execute(
                select(func.count()).select_from(stmt.subquery())
            )
        ).scalar_one()
        result = await self.session.execute(
            stmt.order_by(Member.first_name).limit(limit).offset(offset)
        )
        return list(result.scalars().all()), int(total)
