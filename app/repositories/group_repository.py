from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select, update

from app.models.fee import Fee
from app.models.group import SocialGroup
from app.models.media import Media
from app.models.news import News
from app.models.trustee import Trustee
from app.repositories.base_repository import BaseRepository

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

