from __future__ import annotations

import uuid
from typing import Any, Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.models.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, **kwargs: Any) -> ModelT:
        instance = self.model(**kwargs)
        self.session.add(instance)
        await self.session.flush()
        await self.session.refresh(instance)
        return instance

    async def get_by_id(self, instance_id: uuid.UUID) -> ModelT | None:
        result = await self.session.execute(
            select(self.model).where(self.model.id == instance_id)
        )
        return result.scalar_one_or_none()

    async def list_all(self, limit: int = 100, offset: int = 0) -> list[ModelT]:
        result = await self.session.execute(
            select(self.model)
            .order_by(self.model.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def update(self, instance: ModelT, **kwargs: Any) -> ModelT:
        for key, value in kwargs.items():
            setattr(instance, key, value)
        await self.session.flush()
        await self.session.refresh(instance)
        return instance

    async def delete(self, instance: ModelT, hard: bool = False) -> None:
        if hasattr(instance, "soft_delete") and not hard:
            instance.soft_delete()
        else:
            await self.session.delete(instance)
        await self.session.flush()

    def apply_filters(self, stmt: Select, filters: dict[str, Any]) -> Select:
        for key, value in filters.items():
            if value is not None:
                stmt = stmt.where(getattr(self.model, key) == value)
        return stmt
