"""Today's birthdays and wedding anniversaries.

A member's own date of birth is a column (``members.date_of_birth``), but a
spouse's date of birth and the marriage anniversary are profile values that
were imported from the registration sheet, so they live in
``members.profile_data`` (and, for a spouse, may also sit on the ``family_members``
row). Dates arrive in two spellings - ISO ``YYYY-MM-DD`` written by this API and
the sheet's ``DD/MM/YYYY`` - so both are accepted and anything unparseable is
skipped rather than failing the request.

The SQL pass only narrows the candidate set (it would miss a legacy
``DD/MM/YYYY`` value if it tried to match exactly); the authority is the fully
built profile, which resolves every fallback the member directory already
applies.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from threading import Lock
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.scope import AccessScope
from app.models.family import FamilyMember
from app.models.member import Member
from app.models.member_group import MemberGroupMembership
from app.schemas.celebration import (
    AnniversaryOut,
    BirthdayOut,
    CelebrationLocationOut,
    CelebrationPersonOut,
)
from app.services.profile_service import ProfileService

#: Relationship labels a spouse may be stored under (the form is inconsistent).
SPOUSE_ALIASES = ("spouse", "wife", "husband")

#: Everything ``ProfileService.build_profile`` reads, eager loaded so the async
#: session never attempts a lazy load while building a celebration card.
CELEBRATION_LOADS = (
    selectinload(Member.group),
    selectinload(Member.location),
    selectinload(Member.user),
    selectinload(Member.family_members),
    selectinload(Member.professional_info),
)

_CELEBRATION_CACHE_TTL = 120.0  # 2 minutes
_BIRTHDAY_CACHE: dict[tuple, tuple[float, list[BirthdayOut]]] = {}
_ANNIVERSARY_CACHE: dict[tuple, tuple[float, list[AnniversaryOut]]] = {}
_CELEBRATION_CACHE_LOCK = Lock()


def clear_celebration_cache() -> None:
    with _CELEBRATION_CACHE_LOCK:
        _BIRTHDAY_CACHE.clear()
        _ANNIVERSARY_CACHE.clear()


def _parse_date(value: Any) -> date | None:
    """A date in any of the spellings the registration data arrives in.

    ``profile_data`` mixes ISO ``YYYY-MM-DD``, the US form ``M/D/YYYY`` written
    by the English Google Form, and Indian ``DD-MM-YYYY`` cells typed by hand.
    The separator disambiguates a middle-of-the-road value (``12/01/2000`` is
    US month-first, ``12-01-2000`` is day-first); a part over 12 settles it
    outright. Returns ``None`` for a blank/unrecognised value so one bad cell
    skips that member instead of failing the whole listing.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    text = text.split("T", 1)[0].split(" ", 1)[0]
    separator = "/" if "/" in text else "." if "." in text else "-"
    parts = text.replace(".", "-").replace("/", "-").split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    first, second, third = (int(part) for part in parts)
    if len(parts[0]) == 4:  # YYYY-MM-DD
        year, month, day = first, second, third
    elif first > 12:  # unambiguous day-first: DD-MM-YYYY
        year, month, day = third, second, first
    elif second > 12:  # unambiguous month-first: M/D/YYYY
        year, month, day = third, first, second
    elif separator == "/":  # ambiguous: the English form is month-first
        year, month, day = third, first, second
    else:  # ambiguous dash/dot: the Indian form is day-first
        year, month, day = third, second, first
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _same_day(value: date | None, target: date) -> bool:
    """Whether ``value`` falls on ``target``'s month/day.

    A Feb 29 birthday is matched on Feb 28 in a non-leap year, when that is the
    month's last day, so those members still surface in ordinary years.
    """
    if value is None:
        return False
    if value.month == target.month and value.day == target.day:
        return True
    feb_29_on_28 = (
        target.month == 2 and target.day == 28 and value.month == 2 and value.day == 29
    )
    # Feb 29 exists only in leap years; only celebrate it early in a common year.
    try:
        date(target.year, 2, 29)
    except ValueError:
        return feb_29_on_28
    return False


class CelebrationService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.profiles = ProfileService(session)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def birthdays(self, scope: AccessScope, target: date) -> list[BirthdayOut]:
        cache_key = (
            target.isoformat(),
            scope.unrestricted,
            str(scope.group_id),
            str(scope.member_id),
        )
        now = time.time()
        with _CELEBRATION_CACHE_LOCK:
            if cache_key in _BIRTHDAY_CACHE:
                cached_time, cached_items = _BIRTHDAY_CACHE[cache_key]
                if now - cached_time < _CELEBRATION_CACHE_TTL:
                    return list(cached_items)

        members = await self._candidates(scope, self._birthday_candidates())
        items: list[BirthdayOut] = []
        for member in members:
            profile = self.profiles.build_profile(member)
            member_person = _member_person(member, profile)
            spouse_person = _spouse_person(member, profile)
            member_dob = _parse_date(profile.member_dob) or member.date_of_birth
            common = _common_fields(member)
            if _same_day(member_dob, target):
                items.append(
                    BirthdayOut(
                        occasion="member_birthday",
                        date=target,
                        member=member_person,
                        spouse=spouse_person,
                        **common,
                    )
                )
            if spouse_person is not None and _same_day(
                spouse_person.date_of_birth, target
            ):
                items.append(
                    BirthdayOut(
                        occasion="spouse_birthday",
                        date=target,
                        member=member_person,
                        spouse=spouse_person,
                        **common,
                    )
                )
        with _CELEBRATION_CACHE_LOCK:
            _BIRTHDAY_CACHE[cache_key] = (now, items)
        return items

    async def anniversaries(
        self, scope: AccessScope, target: date
    ) -> list[AnniversaryOut]:
        cache_key = (
            target.isoformat(),
            scope.unrestricted,
            str(scope.group_id),
            str(scope.member_id),
        )
        now = time.time()
        with _CELEBRATION_CACHE_LOCK:
            if cache_key in _ANNIVERSARY_CACHE:
                cached_time, cached_items = _ANNIVERSARY_CACHE[cache_key]
                if now - cached_time < _CELEBRATION_CACHE_TTL:
                    return list(cached_items)

        members = await self._candidates(scope, self._anniversary_candidates())
        items: list[AnniversaryOut] = []
        for member in members:
            profile = self.profiles.build_profile(member)
            anniversary = _parse_date(profile.anniversary_date)
            if not _same_day(anniversary, target):
                continue
            years = target.year - anniversary.year if anniversary.year else None
            items.append(
                AnniversaryOut(
                    date=target,
                    years=years,
                    member=_member_person(member, profile),
                    spouse=_spouse_person(member, profile),
                    **_common_fields(member),
                )
            )
        with _CELEBRATION_CACHE_LOCK:
            _ANNIVERSARY_CACHE[cache_key] = (now, items)
        return items

    # ------------------------------------------------------------------
    # query building
    # ------------------------------------------------------------------
    async def _candidates(self, scope: AccessScope, has_dates) -> list[Member]:
        """Every scoped member that can possibly have a date to compare.

        The SQL pass only keeps members that carry some date at all; it can not
        compare the dates itself because ``profile_data`` mixes formats
        (``M/D/YYYY``, ``DD-MM-YYYY``, ISO) that a LIKE or a cast would read
        wrongly. The exact month/day test is done in Python against the fully
        built profile, which is the same value the member directory displays.
        """
        stmt = (
            select(Member)
            .options(*CELEBRATION_LOADS)
            .where(Member.is_deleted.is_(False), *_scope_conditions(scope), has_dates)
        )
        result = await self.session.execute(stmt.order_by(Member.first_name))
        return list(result.scalars().all())

    @staticmethod
    def _birthday_candidates():
        return or_(
            Member.date_of_birth.isnot(None),
            Member.profile_data["member_dob"].astext.isnot(None),
            Member.profile_data["spouse_dob"].astext.isnot(None),
            Member.family_members.any(
                and_(
                    FamilyMember.date_of_birth.isnot(None),
                    func.lower(func.trim(FamilyMember.relationship_type)).in_(
                        SPOUSE_ALIASES
                    ),
                )
            ),
        )

    @staticmethod
    def _anniversary_candidates():
        return Member.profile_data["anniversary_date"].astext.isnot(None)


def _scope_conditions(scope: AccessScope) -> list:
    """Read scope, matching the member directory's rules.

    A SUPER_ADMIN sees everyone; a GROUP_ADMIN only their group; a MEMBER with
    no group only their own row.
    """
    if scope.unrestricted:
        return []
    if scope.group_id is not None:
        return [
            select(MemberGroupMembership.id)
            .where(
                MemberGroupMembership.member_id == Member.id,
                MemberGroupMembership.group_id == scope.group_id,
            )
            .exists()
        ]
    if scope.member_id is not None:
        return [Member.id == scope.member_id]
    return []


def _spouse_family_member(member: Member) -> FamilyMember | None:
    for item in sorted(
        member.family_members,
        key=lambda value: value.created_at.isoformat() if value.created_at else "",
    ):
        if item.relationship_type.strip().casefold() in SPOUSE_ALIASES:
            return item
    return None


def _member_person(member: Member, profile) -> CelebrationPersonOut:
    return CelebrationPersonOut(
        id=member.id,
        full_name=profile.full_name,
        photo_url=profile.member_photo_link or member.profile_photo_url,
        date_of_birth=_parse_date(profile.member_dob) or member.date_of_birth,
    )


def _spouse_person(member: Member, profile) -> CelebrationPersonOut | None:
    name = profile.spouse_name
    photo = profile.spouse_photo_link
    birthday = _parse_date(profile.spouse_dob)
    if not any((name, photo, birthday)):
        return None
    spouse = _spouse_family_member(member)
    return CelebrationPersonOut(
        id=spouse.id if spouse else None,
        full_name=name,
        photo_url=photo,
        date_of_birth=birthday,
    )


def _common_fields(member: Member) -> dict[str, Any]:
    """Group and location of the member's card, ignoring soft-deleted rows."""
    group = member.group
    if group is not None and group.is_deleted:
        group = None
    location = member.location
    if location is not None and location.is_deleted:
        location = None
    return {
        "group_id": group.id if group else None,
        "group_name": group.name if group else None,
        "location": (
            CelebrationLocationOut.model_validate(location) if location else None
        ),
    }
