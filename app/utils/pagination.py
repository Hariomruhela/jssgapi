from __future__ import annotations

import math
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


class Paginator(Generic[T]):
    def __init__(
        self,
        session: AsyncSession,
        stmt: Select,
        page: int = 1,
        page_size: int = 20,
        max_page_size: int = 100,
    ):
        self.session = session
        self.stmt = stmt
        self.page = max(1, page)
        self.page_size = min(max(1, page_size), max_page_size)

    async def paginate(self) -> dict[str, Any]:
        count_stmt = select(func.count()).select_from(self.stmt.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        total_pages = math.ceil(total / self.page_size) if total else 0
        offset = (self.page - 1) * self.page_size

        result = await self.session.execute(
            self.stmt.limit(self.page_size).offset(offset)
        )
        items = list(result.scalars().all())

        return {
            "data": items,
            "pagination": {
                "page": self.page,
                "page_size": self.page_size,
                "total": total,
                "total_pages": total_pages,
            },
        }


def normalize_page_params(page: int | None, page_size: int | None) -> tuple[int, int]:
    safe_page = max(1, int(page or 1))
    safe_size = min(max(1, int(page_size or 20)), 100)
    return safe_page, safe_size
