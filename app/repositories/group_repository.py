from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select, update

from app.models.fee import Fee
from app.models.group import SocialGroup
from app.models.location import Location
from app.models.media import Media
from app.models.member import Member
from app.models.news import News
from app.models.trustee import Trustee
from app.repositories.base_repository import BaseRepository
from app.utils.helpers import is_valid_uuid

#: Tables whose ``group_id`` FK is ``ON DELETE CASCADE``. A hard delete would
#: silently destroy these rows, so the delete route checks them first and
#: unlinks them instead when it is explicitly allowed to proceed.
CASCADE_DEPENDENT_MODELS = (News, Fee, Trustee, Media)


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

    async def search_detailed(
        self,
        query: str,
        group_id=None,
        status: str | None = None,
        location: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[tuple[SocialGroup, str | None, int]], int]:
        """Keyword search across a group's own fields, its location and members.

        A group matches when the keyword appears in its name/description/contact
        details, in its linked location, or in any of its members - so searching
        a person's name returns the groups that person belongs to. Each hit
        carries the location name and the live member count for display. A UUID
        keyword also matches the group id. ``group_id``/``status``/``location``
        narrow the result; scope is passed as ``group_id`` by the caller.
        """
        term = f"%{query}%"
        location_needle = f"%{location}%" if location else None

        member_match = (
            select(Member.id)
            .where(
                Member.group_id == SocialGroup.id,
                Member.is_deleted.is_(False),
                or_(
                    Member.first_name.ilike(term),
                    Member.middle_name.ilike(term),
                    Member.last_name.ilike(term),
                    Member.membership_number.ilike(term),
                    Member.contact_email.ilike(term),
                    Member.contact_phone.ilike(term),
                    Member.profile_data["full_name"].astext.ilike(term),
                    Member.profile_data["city"].astext.ilike(term),
                    Member.profile_data["area"].astext.ilike(term),
                ),
            )
            .correlate(SocialGroup)
            .exists()
        )
        location_match = SocialGroup.location_id.in_(
            select(Location.id).where(
                or_(
                    Location.name.ilike(term),
                    Location.city.ilike(term),
                    Location.area.ilike(term),
                    Location.state.ilike(term),
                    Location.pincode.ilike(term),
                )
            )
        )
        conditions = [
            SocialGroup.name.ilike(term),
            SocialGroup.name_hi.ilike(term),
            SocialGroup.description.ilike(term),
            SocialGroup.address.ilike(term),
            SocialGroup.contact_name.ilike(term),
            SocialGroup.contact_email.ilike(term),
            SocialGroup.contact_phone.ilike(term),
            SocialGroup.status.ilike(term),
            location_match,
            member_match,
        ]
        if is_valid_uuid(query):
            conditions.append(SocialGroup.id == uuid.UUID(query))

        member_count = (
            select(func.count(Member.id))
            .where(Member.group_id == SocialGroup.id, Member.is_deleted.is_(False))
            .correlate(SocialGroup)
            .scalar_subquery()
        )
        stmt = (
            select(SocialGroup, Location.name, member_count)
            .outerjoin(Location, Location.id == SocialGroup.location_id)
            .where(SocialGroup.is_deleted.is_(False), or_(*conditions))
        )
        if group_id is not None:
            stmt = stmt.where(SocialGroup.id == group_id)
        if status is not None:
            stmt = stmt.where(SocialGroup.status == status)
        if location_needle is not None:
            stmt = stmt.where(
                SocialGroup.location_id.in_(
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
            stmt.order_by(SocialGroup.name).limit(limit).offset(offset)
        )
        rows = [(row[0], row[1], int(row[2] or 0)) for row in result.all()]
        return rows, int(total)

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

    async def count_live_dependents(self, group_id: uuid.UUID) -> dict[str, int]:
        """Non-deleted rows per cascade table that point at this group.

        Soft-deleted dependents are ignored: they are already hidden, so a
        group can still be removed when only a soft-deleted file or article
        references it.
        """
        counts: dict[str, int] = {}
        for model in CASCADE_DEPENDENT_MODELS:
            stmt = select(func.count()).select_from(model).where(
                model.group_id == group_id, model.is_deleted.is_(False)
            )
            counts[model.__tablename__] = int(
                (await self.session.execute(stmt)).scalar_one()
            )
        return counts

    async def hard_delete(self, group: SocialGroup) -> None:
        """Physically delete a group, unlinked from its cascade dependents.

        Every ``CASCADE_DEPENDENT_MODELS`` row that points at the group is
        unlinked (``group_id = NULL``) first, so the FK cascade has nothing to
        destroy. ``members``/``users``/``advertisements``/``events`` are
        ``ON DELETE SET NULL`` and unlink themselves.
        """
        for model in CASCADE_DEPENDENT_MODELS:
            await self.session.execute(
                update(model).where(model.group_id == group.id).values(group_id=None)
            )
        await self.session.delete(group)
        await self.session.flush()

