"""Geo master-data read endpoints: States/UTs and their cities.

Follows the conventions of ``locations.py``: master-data reads are public
(no permission dependency) and use the standard ``ok``/``paginated`` envelopes.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.exceptions import NotFoundException
from app.core.responses import ok, paginated
from app.dependencies import DBSession
from app.models.geo import GeoCity, GeoState
from app.schemas.geo import GeoCityOut, GeoStateOut

router = APIRouter(prefix="/geo", tags=["Geo"])


def _normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


async def _find_state(db: DBSession, state: str) -> GeoState:
    """Resolve a state by LGD name or two-letter code; 404 when unknown.

    Loads the (tiny) table and matches normalized input in Python, the same
    way ``group_id_service._find_state`` does, so both endpoints accept the
    same spellings.
    """
    wanted = _normalize(state)
    if not wanted:
        raise NotFoundException("State", state)
    result = await db.execute(
        select(GeoState).where(GeoState.is_active.is_(True))
    )
    for candidate in result.scalars().all():
        if _normalize(candidate.name) == wanted or candidate.code.lower() == wanted:
            return candidate
    raise NotFoundException("State", state)


@router.get("/states")
async def list_states(
    db: DBSession,
    query: str | None = Query(default=None, max_length=100),
    union_territory: bool | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=100),
):
    """All configured States/UTs (28 states + 8 union territories)."""
    stmt = select(GeoState).where(GeoState.is_active.is_(True))
    if query:
        pattern = f"%{query.strip()}%"
        # Parentheses matter: Python's ``|`` binds tighter than ``==``.
        stmt = stmt.where(
            GeoState.name.ilike(pattern)
            | (GeoState.code == query.strip().upper())
        )
    if union_territory is not None:
        stmt = stmt.where(GeoState.is_union_territory.is_(union_territory))
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(GeoState.name).limit(page_size).offset((page - 1) * page_size)
    )
    items = [GeoStateOut.model_validate(s) for s in result.scalars().all()]
    return paginated(
        "States fetched successfully",
        items,
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.get("/states/{state}/cities")
async def list_cities(
    state: str,
    db: DBSession,
    query: str | None = Query(default=None, max_length=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """Cities/towns (LGD Urban Local Bodies) belonging to one State/UT.

    ``state`` may be the canonical LGD name ("Madhya Pradesh") or the
    two-letter prefix code ("MP"). Unknown states return 404.
    """
    state_row = await _find_state(db, state)
    stmt = select(GeoCity).where(
        GeoCity.state_id == state_row.id, GeoCity.is_active.is_(True)
    )
    if query:
        stmt = stmt.where(GeoCity.name_norm == _normalize(query))
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.options(selectinload(GeoCity.district))
        .order_by(GeoCity.name)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    items: list[GeoCityOut] = []
    for city in result.scalars().all():
        out = GeoCityOut.model_validate(city)
        out.district_name = city.district.name if city.district else None
        items.append(out)
    return paginated(
        f"Cities of {state_row.name} fetched successfully",
        items,
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.get("/states/{state}/cities/{city_lgd_code}")
async def get_city(state: str, city_lgd_code: int, db: DBSession):
    """One city by its official LGD code."""
    state_row = await _find_state(db, state)
    city = await db.scalar(
        select(GeoCity)
        .options(selectinload(GeoCity.district))
        .where(
            GeoCity.lgd_code == city_lgd_code,
            GeoCity.state_id == state_row.id,
            GeoCity.is_active.is_(True),
        )
    )
    if city is None:
        raise NotFoundException("City", str(city_lgd_code))
    out = GeoCityOut.model_validate(city)
    out.district_name = city.district.name if city.district else None
    return ok("City fetched successfully", out)
