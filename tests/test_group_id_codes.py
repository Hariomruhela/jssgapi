"""The Group-ID city codes: Bhopal ``04``, Ujjain ``13``, Indore ``09``.

The city codes are *assigned*, never derived: they come from the explicit
mapping in ``app.core.city_codes`` and the checked-in seed data, not from a
row id, a name, an ASCII value or a position in a list. These tests pin down
that the assigned codes reach the database, the seed file and the generated
IDs - ``MP04001`` for the first group in Bhopal, ``MP13001`` for Ujjain - and
that a counter which lags behind the stored IDs never hands out a number that
is already taken.
"""

from __future__ import annotations

import csv
import re
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.city_codes import REHOMED_CITY_CODES, REQUIRED_CITY_CODES
from app.core.security import hash_password
from app.main import app
from app.models.geo import GeoCity, GeoState
from app.models.group import GroupIdCounter, SocialGroup
from app.models.user import Role, User
from app.services.group_id_service import next_group_id, resolve_city

PASSWORD = "StrongPass123!"

BHOPAL = {"state": "Madhya Pradesh", "city": "Bhopal"}
UJJAIN = {"state": "Madhya Pradesh", "city": "Ujjain"}
INDORE = {"state": "Madhya Pradesh", "city": "Indore"}
NASHIK = {"state": "Maharashtra", "city": "Nashik"}

#: The city codes this project requires, by city name.
EXPECTED_CODES = {
    "Bhopal": "04",
    "Ujjain": "13",
    "Indore": "09",
    "Nashik": "15",
}

#: The cities that gave their code up to a required city.
EXPECTED_REHOMED_CODES = {"Churhat": "414", "Sheopur": "415"}

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "geo"

_created_groups: list[str] = []


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


@pytest.fixture(scope="module")
def _engine():
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        yield engine
    finally:
        engine.dispose()


def _sequence_of(group_id: str, prefix: str) -> int:
    assert group_id.startswith(prefix), group_id
    return int(group_id[len(prefix) :])


def _counter_value(engine, city_key: str) -> int:
    with Session(engine) as session:
        row = session.get(GroupIdCounter, city_key)
    return int(row.last_value) if row else 0


@asynccontextmanager
async def _isolated_session() -> AsyncIterator[AsyncSession]:
    """A session on a connection of its own, opened in the test's event loop."""
    isolated_engine = create_async_engine(
        get_settings().database_url,
        poolclass=NullPool,
        connect_args={"ssl": "require", "statement_cache_size": 0},
    )
    factory = async_sessionmaker(
        isolated_engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with factory() as session:
            yield session
    finally:
        await isolated_engine.dispose()


def _create_group(client: TestClient, auth: dict, name: str, **place) -> dict:
    payload = {"name": name, **(place or BHOPAL)}
    response = client.post("/api/v1/groups", json=payload, headers=auth)
    assert response.status_code in (200, 201), response.text
    group = response.json()["data"]
    _created_groups.append(group["id"])
    return group


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client
        assert test_client.portal is not None
        test_client.portal.call(_dispose_app_engine)


async def _dispose_app_engine() -> None:
    from app.database import engine

    await engine.dispose()


@pytest.fixture(scope="module")
def _mock_firebase():
    from unittest import mock

    def _fake_verify_id_token(id_token: str) -> dict:
        return {"uid": f"groupcodes-{id_token}", "phone_number": id_token}

    with mock.patch(
        "app.services.auth_service.firebase_verify_id_token", _fake_verify_id_token
    ):
        yield


@pytest.fixture(scope="module")
def auth(client: TestClient, _mock_firebase) -> dict:
    """A logged-in SUPER_ADMIN."""
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    phone = f"+918{str(time.time_ns())[-9:]}"
    try:
        with Session(engine) as session:
            role = session.execute(
                select(Role).where(Role.name == "SUPER_ADMIN")
            ).scalar_one()
            session.add(
                User(
                    phone_number=phone,
                    password_hash=hash_password(PASSWORD),
                    full_name="Group Code Admin",
                    is_email_verified=True,
                    is_phone_verified=True,
                    role_id=role.id,
                )
            )
            session.commit()
        response = client.post(
            "/api/v1/auth/login",
            json={"phone_number": phone, "password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        yield {"Authorization": f"Bearer {response.json()['tokens']['access_token']}"}
    finally:
        with Session(engine) as session:
            session.execute(delete(User).where(User.phone_number == phone))
            session.commit()
        engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def _cleanup(_engine):
    yield
    if not _created_groups:
        return
    with Session(_engine) as session:
        session.execute(
            delete(SocialGroup).where(SocialGroup.id.in_(_created_groups))
        )
        session.commit()


# --- the assigned codes -----------------------------------------------------


def test_required_codes_are_stored_in_the_geo_master_data(_engine):
    """Every required city carries its assigned code and the matching prefix."""
    with Session(_engine) as session:
        for lgd_code, expected in REQUIRED_CITY_CODES.items():
            row = session.execute(
                select(GeoCity, GeoState)
                .join(GeoState, GeoState.id == GeoCity.state_id)
                .where(GeoCity.lgd_code == lgd_code)
            ).first()
            assert row is not None, f"LGD {lgd_code} is missing from the master data"
            city, state = row
            assert city.city_code == expected, (city.name, city.city_code)
            assert city.prefix == f"{state.code}{expected}", (city.name, city.prefix)


def test_seed_file_carries_the_required_codes():
    """The checked-in seed data agrees with the required mapping."""
    with (DATA_DIR / "cities.csv").open(newline="", encoding="utf-8") as handle:
        rows = {int(row["lgd_code"]): row for row in csv.DictReader(handle)}
    for lgd_code, expected in {**REQUIRED_CITY_CODES, **REHOMED_CITY_CODES}.items():
        assert rows[lgd_code]["city_code"] == expected, rows[lgd_code]


def test_rehomed_cities_keep_their_new_codes(_engine):
    """Churhat and Sheopur moved out of the way of Bhopal and Ujjain."""
    with Session(_engine) as session:
        for lgd_code, expected in REHOMED_CITY_CODES.items():
            row = session.execute(
                select(GeoCity).where(GeoCity.lgd_code == lgd_code)
            ).scalar_one()
            assert row.city_code == expected, (row.name, row.city_code)
        # The codes the required cities took over are used by exactly one city.
        for lgd_code in REQUIRED_CITY_CODES:
            city = session.execute(
                select(GeoCity).where(GeoCity.lgd_code == lgd_code)
            ).scalar_one()
            owners = session.execute(
                select(GeoCity).where(
                    GeoCity.city_code == city.city_code,
                    GeoCity.state_id == city.state_id,
                )
            ).scalars().all()
            assert [owner.name for owner in owners] == [city.name]


# --- the generated IDs -------------------------------------------------------


async def test_the_first_bhopal_group_is_mp04001():
    """The first group created in Bhopal gets ``MP04001``."""
    async with _isolated_session() as session:
        city = await resolve_city(session, "Madhya Pradesh", "Bhopal")
        assert city.city_code == "04"
        assert city.prefix == "MP04"
        counter = await session.get(GroupIdCounter, "MP04")
        assert counter is None or counter.last_value == 0
        assert await next_group_id(session, city) == "MP04001"
        # The number goes back to the pool - the test leaves no trace.
        await session.rollback()


async def test_the_first_ujjain_group_is_mp13001():
    """The first group created in Ujjain gets ``MP13001``."""
    async with _isolated_session() as session:
        city = await resolve_city(session, "Madhya Pradesh", "Ujjain")
        assert city.city_code == "13"
        assert city.prefix == "MP13"
        counter = await session.get(GroupIdCounter, "MP13")
        assert counter is None or counter.last_value == 0
        assert await next_group_id(session, city) == "MP13001"
        await session.rollback()


def test_bhopal_group_gets_the_next_mp04_id(client, auth, _engine):
    before = _counter_value(_engine, "MP04")
    group = _create_group(client, auth, f"Bhopal Group {uuid4().hex[:8]}", **BHOPAL)

    assert re.fullmatch(r"MP04\d{3,}", group["group_id"]), group["group_id"]
    assert group["state"] == "Madhya Pradesh"
    assert group["city"] == "Bhopal"
    assert _sequence_of(group["group_id"], "MP04") == before + 1


def test_ujjain_group_gets_the_next_mp13_id(client, auth, _engine):
    before = _counter_value(_engine, "MP13")
    group = _create_group(client, auth, f"Ujjain Group {uuid4().hex[:8]}", **UJJAIN)

    assert re.fullmatch(r"MP13\d{3,}", group["group_id"]), group["group_id"]
    assert group["state"] == "Madhya Pradesh"
    assert group["city"] == "Ujjain"
    assert _sequence_of(group["group_id"], "MP13") == before + 1


def test_indore_and_nashik_keep_their_codes(client, auth):
    """The codes that were already right are untouched."""
    indore = _create_group(client, auth, f"Indore Group {uuid4().hex[:8]}", **INDORE)
    nashik = _create_group(client, auth, f"Nashik Group {uuid4().hex[:8]}", **NASHIK)

    assert indore["group_id"].startswith("MP09"), indore["group_id"]
    assert nashik["group_id"].startswith("MH15"), nashik["group_id"]


def test_concurrent_bhopal_groups_never_share_an_id(client, auth):
    stamp = uuid4().hex[:8]
    names = [f"Concurrent Bhopal {stamp} #{i}" for i in range(6)]

    def _create(name: str) -> dict:
        response = client.post(
            "/api/v1/groups", json={**BHOPAL, "name": name}, headers=auth
        )
        assert response.status_code in (200, 201), response.text
        return response.json()["data"]

    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        groups = list(pool.map(_create, names))

    ids = [group["group_id"] for group in groups]
    assert len(set(ids)) == len(names), ids
    sequences = sorted(_sequence_of(group_id, "MP04") for group_id in ids)
    assert sequences == list(range(sequences[0], sequences[0] + len(names))), sequences
    _created_groups.extend(group["id"] for group in groups)


def test_a_lagging_counter_never_reuses_a_stored_number(client, auth, _engine):
    """A stored ID that is higher than the counter must never be handed out.

    The counter can lag behind the rows - a restored database, a corrected
    city code, an ID written by hand. The allocation raises the counter to the
    highest sequence the city already uses instead of repeating it.
    """
    stamp = uuid4().hex[:8]
    taken_id = f"MP04{999:03d}"
    with Session(_engine) as session:
        row = SocialGroup(
            name=f"Lagging Counter {stamp}",
            status="ACTIVE",
            is_active=True,
            group_id=taken_id,
            state="Madhya Pradesh",
            city="Bhopal",
        )
        session.add(row)
        session.commit()
        row_id = row.id
    try:
        group = _create_group(client, auth, f"Bhopal After Lag {stamp}", **BHOPAL)
        assert _sequence_of(group["group_id"], "MP04") > 999, group["group_id"]
    finally:
        with Session(_engine) as session:
            session.execute(delete(SocialGroup).where(SocialGroup.id == row_id))
            session.commit()
