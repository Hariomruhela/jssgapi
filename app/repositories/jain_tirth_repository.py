from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, or_, select

from app.core.constants import EventStatus
from app.models.jain_tirth import JainTirth
from app.repositories.base_repository import BaseRepository


class JainTirthRepository(BaseRepository[JainTirth]):
    model = JainTirth

    async def get_by_id(self, instance_id: UUID) -> JainTirth | None:
        result = await self.session.execute(
            select(JainTirth).where(
                JainTirth.id == instance_id,
                JainTirth.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    def _clauses(
        self,
        *,
        group_id: UUID | None,
        ungrouped_only: bool,
        include_ungrouped: bool,
        status: EventStatus | None,
        search: str | None,
        public_only: bool = False,
    ) -> list[Any]:
        clauses: list[Any] = [JainTirth.is_deleted.is_(False)]
        if group_id is not None:
            clauses.append(JainTirth.group_id == group_id)
        elif ungrouped_only:
            clauses.append(JainTirth.group_id.is_(None))
        elif not include_ungrouped:
            clauses.append(JainTirth.group_id.is_not(None))
        if status is not None:
            clauses.append(JainTirth.status == status.value)
        if search:
            term = f"%{search.strip()}%"
            clauses.append(
                or_(
                    JainTirth.title.ilike(term),
                    JainTirth.description.ilike(term),
                    JainTirth.location.ilike(term),
                )
            )
        if public_only:
            clauses.append(JainTirth.is_public.is_(True))
        return clauses

    def _statement(
        self,
        *,
        group_id: UUID | None,
        ungrouped_only: bool = False,
        include_ungrouped: bool = True,
        status: EventStatus | None,
        search: str | None,
        public_only: bool = False,
        order: str = "desc",
    ) -> Select:
        stmt = select(JainTirth).where(
            *self._clauses(
                group_id=group_id,
                ungrouped_only=ungrouped_only,
                include_ungrouped=include_ungrouped,
                status=status,
                search=search,
                public_only=public_only,
            )
        )
        ordering = (
            JainTirth.created_at.asc()
            if order == "asc"
            else JainTirth.created_at.desc()
        )
        return stmt.order_by(ordering)

    async def list_filtered(
        self,
        *,
        group_id: UUID | None = None,
        ungrouped_only: bool = False,
        include_ungrouped: bool = True,
        status: EventStatus | None = None,
        search: str | None = None,
        public_only: bool = False,
        order: str = "desc",
        limit: int = 20,
        offset: int = 0,
    ) -> list[JainTirth]:
        stmt = self._statement(
            group_id=group_id,
            ungrouped_only=ungrouped_only,
            include_ungrouped=include_ungrouped,
            status=status,
            search=search,
            public_only=public_only,
            order=order,
        )
        result = await self.session.execute(stmt.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def count_filtered(
        self,
        *,
        group_id: UUID | None = None,
        ungrouped_only: bool = False,
        include_ungrouped: bool = True,
        status: EventStatus | None = None,
        search: str | None = None,
        public_only: bool = False,
    ) -> int:
        stmt = select(JainTirth.id).where(
            *self._clauses(
                group_id=group_id,
                ungrouped_only=ungrouped_only,
                include_ungrouped=include_ungrouped,
                status=status,
                search=search,
                public_only=public_only,
            )
        )
        result = await self.session.execute(stmt)
        return len(result.all())
