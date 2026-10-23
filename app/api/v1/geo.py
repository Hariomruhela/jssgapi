"""RTO-backed geo read endpoints: States/UTs and their Group-ID cities.

Only states in the checked-in RTO mapping are exposed, and city responses carry
the same city code that will be used to mint new Group IDs. Follows the
conventions of ``locations.py``: master-data reads are public (no permission
dependency) and use the standard ``ok``/``paginated`` envelopes.
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
from app.services.rto_service import RTO_STATE_CODES, rto_code_for_city

router = APIRouter(prefix="/geo", tags=["Geo"])


def _normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


def _city_out(city: GeoCity, state_code: str, rto_code: str) -> GeoCityOut:
    """Serialize a city with the RTO code that will appear in its Group ID."""
    out = GeoCityOut.model_validate(city)
    out.city_code = rto_code
    out.prefix = f"{state_code}{rto_code}"
    out.district_name = city.district.name if city.district else None
    return out


async def _find_state(db: DBSession, state: str) -> GeoState:
    """Resolve an RTO-covered state by LGD name or two-letter code.

    Loads the (tiny) table and matches normalized input in Python, the same
    way ``group_id_service._find_state`` does, so both endpoints accept the
    same spellings. States without an RTO mapping are reported as missing.
    """
    wanted = _normalize(state)
    if not wanted:
        raise NotFoundException("State", state)
    result = await db.execute(
        select(GeoState).where(
            GeoState.is_active.is_(True),
            GeoState.code.in_(RTO_STATE_CODES),
        )
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
    """All RTO-covered States/UTs (33 states and union territories)."""
    stmt = select(GeoState).where(
        GeoState.is_active.is_(True),
        GeoState.code.in_(RTO_STATE_CODES),
    )
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
    """LGD cities/towns in one RTO-covered State/UT that have an RTO code.

    ``state`` may be the canonical LGD name ("Madhya Pradesh") or the
    two-letter prefix code ("MP"). Unknown or non-RTO states return 404.
    The response's ``city_code``/``prefix`` are the RTO code and the exact
    stamp used by new Group IDs.
    """
    state_row = await _find_state(db, state)
    stmt = select(GeoCity).where(
        GeoCity.state_id == state_row.id, GeoCity.is_active.is_(True)
    )
    if query:
        stmt = stmt.where(GeoCity.name_norm == _normalize(query))
    result = await db.execute(
        stmt.options(selectinload(GeoCity.district)).order_by(GeoCity.name)
    )
    matched: list[tuple[GeoCity, str]] = []
    for city in result.scalars().all():
        rto_code = rto_code_for_city(state_row.code, city.name, city.lgd_code)
        if rto_code is not None:
            matched.append((city, rto_code))
    total = len(matched)
    start = (page - 1) * page_size
    items = [
        _city_out(city, state_row.code, rto_code)
        for city, rto_code in matched[start : start + page_size]
    ]
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
    """One RTO-mapped city by its official LGD code."""
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
    rto_code = rto_code_for_city(state_row.code, city.name, city.lgd_code)
    if rto_code is None:
        raise NotFoundException("City", str(city_lgd_code))
    return ok(
        "City fetched successfully",
        _city_out(city, state_row.code, rto_code),
    )
