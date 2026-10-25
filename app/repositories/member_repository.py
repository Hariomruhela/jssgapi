from __future__ import annotations

from uuid import UUID

from sqlalchemy import String, and_, case, cast, func, or_, select
from sqlalchemy.orm import selectinload

from app.core.constants import LEGACY_ROLE_ALIASES, RoleName
from app.models.family import FamilyMember
from app.models.group import SocialGroup
from app.models.location import Location
from app.models.member import Member
from app.models.professional import ProfessionalInformation
from app.models.user import Role, User
from app.repositories.base_repository import BaseRepository
from app.utils.helpers import is_valid_uuid

# ``Member.group`` and ``Member.location`` use the default lazy strategy, which
# cannot be resolved inside an async session. Any query whose caller needs the
# profile fields (social_group_name, city, area) must eager load them.
MEMBER_PROFILE_LOADS = (
    selectinload(Member.group),
    selectinload(Member.location),
)

#: The roles whose holders are trustees of the group their member row belongs to.
TRUSTEE_ROLES = frozenset({RoleName.SUPER_ADMIN, RoleName.GROUP_ADMIN})

#: Every stored role label that resolves to a trustee role, including the
#: retired aliases that old imports may still carry. Matching the raw labels in
#: SQL keeps the filter working even when a row predates the role cleanup.
_TRUSTEE_ROLE_LABELS_BY_ROLE: dict[RoleName, frozenset[str]] = {
    role: frozenset(
        {role.value}
        | {
            label
            for label, resolved in LEGACY_ROLE_ALIASES.items()
            if resolved is role
        }
    )
    for role in TRUSTEE_ROLES
}
TRUSTEE_ROLE_LABELS = frozenset(
    label for labels in _TRUSTEE_ROLE_LABELS_BY_ROLE.values() for label in labels
)


def trustee_role_filter(role: RoleName | None = None):
    """Members whose linked login account is a group or platform administrator.

    A trustee is a community member whose account carries ``GROUP_ADMIN`` or
    ``SUPER_ADMIN``; the member is not a second record. The member's own
    ``group_id`` decides which group's trustees they are, so a SUPER_ADMIN is a
    trustee of the group they were added to rather than of every group.
    """
    labels = (
        _TRUSTEE_ROLE_LABELS_BY_ROLE[role] if role is not None else TRUSTEE_ROLE_LABELS
    )
    return (
        select(User.id)
        .join(Role, Role.id == User.role_id)
        .where(
            User.id == Member.user_id,
            User.is_deleted.is_(False),
            func.upper(func.btrim(Role.name, _TRIM_CHARS)).in_(labels),
        )
        .exists()
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

    The designation is kept in ``members.profile_data`` and mirrored into
    ``professional_information.designation``; either store may hold it, so both
    are read. A blank value (``null``, ``""`` or whitespace only) means the
    member has no designation. Being a trustee is decided by the linked
    account's role (see :func:`trustee_role_filter`), not by this field.
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


def _build_member_search_expressions(query: str):
    clean_q = query.strip()
    if not clean_q:
        return None, None

    prefix = f"{clean_q}%"
    contains = f"%{clean_q}%"
    tokens = [t for t in clean_q.split() if t]

    full_name_first_last = func.concat(
        Member.first_name, " ", func.coalesce(Member.last_name, "")
    )
    full_name_with_middle = func.concat(
        Member.first_name,
        " ",
        func.coalesce(Member.middle_name, ""),
        " ",
        func.coalesce(Member.last_name, ""),
    )
    full_name_last_first = func.concat(
        Member.last_name, " ", func.coalesce(Member.first_name, "")
    )

    spouse_rel_types = ["spouse", "wife", "husband"]
    spouse_name_exists = (
        select(FamilyMember.id)
        .where(
            FamilyMember.member_id == Member.id,
            func.lower(FamilyMember.relationship_type).in_(spouse_rel_types),
            FamilyMember.name.ilike(contains),
        )
        .exists()
    )
    spouse_name_prefix_exists = (
        select(FamilyMember.id)
        .where(
            FamilyMember.member_id == Member.id,
            func.lower(FamilyMember.relationship_type).in_(spouse_rel_types),
            FamilyMember.name.ilike(prefix),
        )
        .exists()
    )
    spouse_phone_exists = (
        select(FamilyMember.id)
        .where(
            FamilyMember.member_id == Member.id,
            func.lower(FamilyMember.relationship_type).in_(spouse_rel_types),
            FamilyMember.contact_phone.ilike(contains),
        )
        .exists()
    )

    single_field_conditions = [
        Member.first_name.ilike(contains),
        Member.middle_name.ilike(contains),
        Member.last_name.ilike(contains),
        full_name_first_last.ilike(contains),
        full_name_with_middle.ilike(contains),
        full_name_last_first.ilike(contains),
        Member.profile_data["full_name"].astext.ilike(contains),
        Member.contact_email.ilike(contains),
        Member.contact_phone.ilike(contains),
        Member.membership_number.ilike(contains),
        Member.address_line.ilike(contains),
        Member.occupation_summary.ilike(contains),
        # Spouse fields
        Member.profile_data["spouse_name"].astext.ilike(contains),
        Member.profile_data["spouse_mobile"].astext.ilike(contains),
        spouse_name_exists,
        spouse_phone_exists,
        # Location fields (City, State, Area, Name)
        Location.name.ilike(contains),
        Location.city.ilike(contains),
        Location.state.ilike(contains),
        Location.area.ilike(contains),
        Member.profile_data["city"].astext.ilike(contains),
        Member.profile_data["state"].astext.ilike(contains),
        Member.profile_data["area"].astext.ilike(contains),
        Member.profile_data["address"].astext.ilike(contains),
        # Social Group
        SocialGroup.name.ilike(contains),
        Member.profile_data["social_group_name"].astext.ilike(contains),
        # Occupation / Education
        Member.profile_data["member_occupation"].astext.ilike(contains),
        Member.profile_data["member_education"].astext.ilike(contains),
        Member.profile_data["company_name"].astext.ilike(contains),
        # Member ID & Group ID prefix / contains
        cast(Member.id, String).ilike(f"{clean_q}%"),
        cast(Member.group_id, String).ilike(f"{clean_q}%"),
    ]

    if is_valid_uuid(clean_q):
        parsed_uuid = UUID(clean_q)
        single_field_conditions.extend([
            Member.id == parsed_uuid,
            Member.group_id == parsed_uuid,
        ])

    single_needle_match = or_(*single_field_conditions)

    if len(tokens) > 1:
        token_clauses = []
        for t in tokens:
            t_contains = f"%{t}%"
            t_spouse_exists = (
                select(FamilyMember.id)
                .where(
                    FamilyMember.member_id == Member.id,
                    func.lower(FamilyMember.relationship_type).in_(spouse_rel_types),
                    FamilyMember.name.ilike(t_contains),
                )
                .exists()
            )
            t_cond = or_(
                Member.first_name.ilike(t_contains),
                Member.middle_name.ilike(t_contains),
                Member.last_name.ilike(t_contains),
                Member.profile_data["full_name"].astext.ilike(t_contains),
                Member.profile_data["spouse_name"].astext.ilike(t_contains),
                t_spouse_exists,
                Location.city.ilike(t_contains),
                Location.state.ilike(t_contains),
                Location.name.ilike(t_contains),
                Member.profile_data["city"].astext.ilike(t_contains),
                Member.profile_data["state"].astext.ilike(t_contains),
                SocialGroup.name.ilike(t_contains),
                Member.profile_data["social_group_name"].astext.ilike(t_contains),
                Member.contact_phone.ilike(t_contains),
                Member.membership_number.ilike(t_contains),
                Member.contact_email.ilike(t_contains),
            )
            token_clauses.append(t_cond)
        where_condition = or_(single_needle_match, and_(*token_clauses))
    else:
        where_condition = single_needle_match

    rank_expr = case(
        (Member.first_name.ilike(clean_q), 1),
        (Member.first_name.ilike(prefix), 2),
        (Member.last_name.ilike(prefix), 3),
        (full_name_first_last.ilike(prefix), 4),
        (Member.profile_data["spouse_name"].astext.ilike(prefix), 5),
        (spouse_name_prefix_exists, 5),
        (Location.city.ilike(prefix), 6),
        (Member.profile_data["city"].astext.ilike(prefix), 6),
        (Location.state.ilike(prefix), 7),
        (Member.profile_data["state"].astext.ilike(prefix), 7),
        (SocialGroup.name.ilike(prefix), 8),
        (Member.profile_data["social_group_name"].astext.ilike(prefix), 8),
        (Member.first_name.ilike(contains), 9),
        (Member.last_name.ilike(contains), 10),
        (full_name_first_last.ilike(contains), 11),
        (Member.profile_data["spouse_name"].astext.ilike(contains), 12),
        (spouse_name_exists, 12),
        (Location.city.ilike(contains), 13),
        (Member.profile_data["city"].astext.ilike(contains), 13),
        (Location.state.ilike(contains), 14),
        (SocialGroup.name.ilike(contains), 15),
        (Member.membership_number.ilike(prefix), 16),
        (Member.contact_phone.ilike(prefix), 17),
        (Member.membership_number.ilike(contains), 18),
        (Member.contact_phone.ilike(contains), 19),
        else_=30,
    )

    return where_condition, rank_expr


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

    async def get_by_phone(self, phone: str) -> Member | None:
        normalized = phone.strip()
        local = normalized.removeprefix("+91")
        conditions = [Member.contact_phone == normalized]
        if local != normalized:
            conditions.append(Member.contact_phone == local)
        result = await self.session.execute(
            select(Member).where(
                Member.is_deleted.is_(False),
                or_(*conditions),
            )
        )
        return result.scalars().first()

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
        where_cond, rank_expr = _build_member_search_expressions(query)
        stmt = (
            select(Member)
            .outerjoin(Location, Location.id == Member.location_id)
            .outerjoin(SocialGroup, SocialGroup.id == Member.group_id)
            .options(*MEMBER_PROFILE_LOADS)
            .where(Member.is_deleted.is_(False))
        )
        if where_cond is not None:
            stmt = stmt.where(where_cond)
        if group_id is not None:
            stmt = stmt.where(Member.group_id == group_id)
        if membership_status is not None:
            stmt = stmt.where(Member.membership_status == membership_status)
        if group_name:
            stmt = stmt.where(group_name_filter(group_name))
        if trustee_only:
            stmt = stmt.where(trustee_role_filter())
        if (designation or "").strip():
            stmt = stmt.where(designation_filter(designation.strip()))

        if rank_expr is not None:
            stmt = stmt.order_by(
                rank_expr.asc(), Member.first_name.asc(), Member.last_name.asc()
            )
        else:
            stmt = stmt.order_by(Member.first_name.asc(), Member.last_name.asc())

        result = await self.session.execute(stmt.limit(limit).offset(offset))
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

        Matches the member's own columns, full name combinations, spouse name,
        linked group, linked location (city, state), phone, membership number,
        and profile fields. Natural ranking prioritizes exact/prefix name matches.
        """
        where_cond, rank_expr = _build_member_search_expressions(query)
        location_needle = f"%{location}%" if location else None

        stmt = (
            select(Member)
            .outerjoin(Location, Location.id == Member.location_id)
            .outerjoin(SocialGroup, SocialGroup.id == Member.group_id)
            .options(*MEMBER_PROFILE_LOADS)
            .where(Member.is_deleted.is_(False))
        )
        if where_cond is not None:
            stmt = stmt.where(where_cond)
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
                or_(
                    Location.name.ilike(location_needle),
                    Location.city.ilike(location_needle),
                    Location.area.ilike(location_needle),
                    Location.state.ilike(location_needle),
                    Member.profile_data["city"].astext.ilike(location_needle),
                    Member.profile_data["state"].astext.ilike(location_needle),
                    Member.profile_data["area"].astext.ilike(location_needle),
                )
            )

        total = (
            await self.session.execute(
                select(func.count()).select_from(stmt.subquery())
            )
        ).scalar_one()

        if rank_expr is not None:
            stmt = stmt.order_by(
                rank_expr.asc(), Member.first_name.asc(), Member.last_name.asc()
            )
        else:
            stmt = stmt.order_by(Member.first_name.asc(), Member.last_name.asc())

        result = await self.session.execute(stmt.limit(limit).offset(offset))
        return list(result.scalars().all()), int(total)
