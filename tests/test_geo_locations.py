"""Geo master data: endpoints, location validation and import safety.

``data/geo/*.csv`` (LGD-sourced) seeds ``geo_states`` / ``geo_districts`` /
``geo_cities``; ``POST /groups`` validates every state/city pair against it
before minting ``STATE + CITY + SEQUENCE`` Group IDs. These tests pin:

* the public geo read endpoints (33 RTO States/UTs, RTO-mapped cities, 404s);
* creation-time rejection of unknown state, unknown city, a city from another
  state, and an ambiguous city name that exists twice in the same state;
* an idempotent importer that never rewrites an already-issued prefix
  (``MP09`` Indore, ``MH15`` Nashik) and refuses colliding seed files before
  touching the database.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models.geo import GeoCity, GeoDistrict, GeoState
from app.models.group import SocialGroup
from app.models.user import Role, User
from app.services.geo_service import DATA_DIR, import_geo_data, validate_geo_files
from app.services.group_id_service import resolve_city

PASSWORD = "StrongPass123!"

INDORE = {"state": "Madhya Pradesh", "city": "Indore"}

_created_groups: list[str] = []


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


@asynccontextmanager
async def _isolated_session() -> AsyncIterator[AsyncSession]:
    """A session on a connection of its own (see test_group_ids)."""
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


async def _dispose_app_engine() -> None:
    from app.database import engine

    await engine.dispose()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client
        assert test_client.portal is not None
        test_client.portal.call(_dispose_app_engine)


@pytest.fixture(scope="module")
def _mock_firebase():
    from unittest import mock

    def _fake_verify_id_token(id_token: str) -> dict:
        return {"uid": f"geo-{id_token}", "phone_number": id_token}

    with mock.patch(
        "app.services.auth_service.firebase_verify_id_token", _fake_verify_id_token
    ):
        yield


@pytest.fixture(scope="module")
def auth(client: TestClient, _mock_firebase) -> dict:
    """A logged-in SUPER_ADMIN (the geo read endpoints themselves need none)."""
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
                    full_name="Geo Admin",
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
def _cleanup():
    yield
    if not _created_groups:
        return
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    with Session(engine) as session:
        session.execute(
            delete(SocialGroup).where(SocialGroup.id.in_(_created_groups))
        )
        session.commit()
    engine.dispose()


def _create(client: TestClient, auth: dict, **place) -> dict:
    response = client.post(
        "/api/v1/groups",
        json={"name": f"Geo {uuid4().hex[:8]}", **place},
        headers=auth,
    )
    assert response.status_code in (200, 201), response.text
    group = response.json()["data"]
    _created_groups.append(group["id"])
    return group


# --- geo read endpoints (public) --------------------------------------------


def test_all_33_rto_states_and_ut_listed_without_auth(client):
    response = client.get("/api/v1/geo/states", params={"page_size": 100})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pagination"]["total"] == 33
    items = body["data"]
    codes = {s["code"] for s in items}
    assert codes == {
        "AN", "AP", "AR", "AS", "BR", "CG", "DD", "DL", "GA", "GJ", "HP",
        "HR", "JH", "JK", "KA", "KL", "LD", "MH", "ML", "MN", "MP", "MZ",
        "NL", "OD", "PB", "PY", "RJ", "SK", "TN", "TR", "UK", "UP", "WB",
    }
    assert not codes & {"CH", "LA", "TG"}
    ut_codes = {s["code"] for s in items if s["is_union_territory"]}
    assert ut_codes == {"AN", "DD", "DL", "JK", "LD", "PY"}
    mp = next(s for s in items if s["code"] == "MP")
    assert mp["name"] == "Madhya Pradesh"
    assert mp["lgd_code"] == 23


def test_state_search_and_union_territory_filter(client):
    found = client.get("/api/v1/geo/states", params={"query": "madhya"}).json()
    assert found["pagination"]["total"] == 1
    assert found["data"][0]["code"] == "MP"

    by_code = client.get("/api/v1/geo/states", params={"query": "mh"}).json()
    assert by_code["pagination"]["total"] == 1
    assert by_code["data"][0]["name"] == "Maharashtra"

    no_chandigarh = client.get(
        "/api/v1/geo/states", params={"query": "chandigarh"}
    ).json()
    assert no_chandigarh["pagination"]["total"] == 0

    uts = client.get(
        "/api/v1/geo/states", params={"union_territory": True, "page_size": 100}
    ).json()
    assert uts["pagination"]["total"] == 6


def test_cities_of_state_by_name_and_by_code(client):
    for state in ("Madhya Pradesh", "MP"):
        response = client.get(
            f"/api/v1/geo/states/{state}/cities", params={"page_size": 200}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["pagination"]["total"] == 65
        indore = next(c for c in body["data"] if c["name"] == "Indore")
        assert indore["prefix"] == "MP09"
        assert indore["city_code"] == "09"
        assert indore["district_name"] == "Indore"
        assert all(
            c["city_code"].isdigit()
            and len(c["city_code"]) == 2
            and c["prefix"] == f"MP{c['city_code']}"
            for c in body["data"]
        )
        assert "Churhat" not in {c["name"] for c in body["data"]}


def test_delhi_overrides_and_empty_lakshadweep(client):
    delhi = client.get(
        "/api/v1/geo/states/DL/cities", params={"page_size": 200}
    )
    assert delhi.status_code == 200, delhi.text
    body = delhi.json()
    assert body["pagination"]["total"] == 4
    by_name = {c["name"]: c for c in body["data"]}
    assert by_name["East Delhi Municipal Corporation"]["prefix"] == "DL07"
    assert by_name["North Delhi Municipal Corporation"]["prefix"] == "DL06"
    assert by_name["South Delhi Municipal Corporation"]["prefix"] == "DL03"
    assert by_name["New Delhi Municipal Council"]["prefix"] == "DL02"

    lakshadweep = client.get(
        "/api/v1/geo/states/LD/cities", params={"page_size": 200}
    )
    assert lakshadweep.status_code == 200, lakshadweep.text
    assert lakshadweep.json()["pagination"]["total"] == 0


def test_city_detail_and_404s(client):
    ok = client.get("/api/v1/geo/states/MP/cities/250901")
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["name"] == "Indore"
    assert ok.json()["data"]["prefix"] == "MP09"

    assert client.get("/api/v1/geo/states/MP/cities/248068").status_code == 404
    assert client.get("/api/v1/geo/states/MP/cities/999999").status_code == 404
    assert client.get("/api/v1/geo/states/Kalinga/cities").status_code == 404
    assert client.get("/api/v1/geo/states/%20%20/cities").status_code == 404
    assert client.get("/api/v1/geo/states/CH/cities").status_code == 404
    assert client.get("/api/v1/geo/states/TG/cities").status_code == 404


async def test_delhi_api_prefix_matches_group_id_resolution():
    async with _isolated_session() as session:
        east = await resolve_city(
            session, "Delhi", "East Delhi Municipal Corporation"
        )
        north = await resolve_city(
            session, "Delhi", "North Delhi Municipal Corporation"
        )
        south = await resolve_city(
            session, "Delhi", "South Delhi Municipal Corporation"
        )
        new_delhi = await resolve_city(
            session, "Delhi", "New Delhi Municipal Council"
        )

    assert east.prefix == "DL07"
    assert north.prefix == "DL06"
    assert south.prefix == "DL03"
    assert new_delhi.prefix == "DL02"


# --- location validation at group creation ----------------------------------


def test_chandigarh_city_requires_chandigarh_state(client, auth):
    """The classic mismatch: Chandigarh is a UT *and* a city inside it."""
    wrong = client.post(
        "/api/v1/groups",
        json={
            "name": f"Wrong UT {uuid4().hex[:8]}",
            "state": "Haryana",
            "city": "Chandigarh",
        },
        headers=auth,
    )
    assert wrong.status_code == 422, wrong.text
    assert "does not belong to Haryana" in wrong.json()["message"]
    assert "Chandigarh (Chandigarh)" in wrong.json()["message"]

    group = _create(client, auth, state="Chandigarh", city="Chandigarh")
    assert re.fullmatch(r"CH01\d{3,}", group["group_id"]), group["group_id"]
    assert group["state"] == "Chandigarh"
    assert group["city"] == "Chandigarh"


def test_ambiguous_city_name_is_rejected(client, auth):
    """The LGD has two Itanagar ULBs, so the name alone cannot pick one."""
    response = client.post(
        "/api/v1/groups",
        json={
            "name": f"Ambiguous {uuid4().hex[:8]}",
            "state": "Arunachal Pradesh",
            "city": "Itanagar",
        },
        headers=auth,
    )
    assert response.status_code == 422, response.text
    assert "ambiguous" in response.json()["message"].lower()


def test_two_letter_state_code_is_accepted(client, auth):
    group = _create(client, auth, state="mp", city="INDORE")
    assert group["group_id"].startswith("MP09")
    assert group["state"] == "Madhya Pradesh"
    assert group["city"] == "Indore"


def test_unknown_city_is_rejected(client, auth):
    response = client.post(
        "/api/v1/groups",
        json={
            "name": f"Fake City {uuid4().hex[:8]}",
            "state": "Madhya Pradesh",
            "city": "Atlantis City",
        },
        headers=auth,
    )
    assert response.status_code == 422, response.text
    assert "Atlantis City" in response.json()["message"]


# --- import safety -----------------------------------------------------------


async def test_bootstrap_seeded_the_geo_tables():
    """conftest ran ``bootstrap_app``, whose guarded hook seeds empty tables."""
    async with _isolated_session() as session:
        states = (await session.scalar(select(func.count()).select_from(GeoState))) or 0
        districts = (
            await session.scalar(select(func.count()).select_from(GeoDistrict)) or 0
        )
        cities = (await session.scalar(select(func.count()).select_from(GeoCity))) or 0
    assert states == 36
    assert districts == 784
    assert cities == 5051


async def test_reimport_is_idempotent_and_preserves_pinned_prefixes():
    async with _isolated_session() as session:
        first = await import_geo_data(session)
        await session.flush()
        assert first.cities_created == 0  # already seeded by bootstrap

        second = await import_geo_data(session)
        await session.flush()
        assert second.states_upserted == 0
        assert second.districts_upserted == 0
        assert second.cities_upserted == 0
        assert second.cities_created == 0

        indore = (
            await session.execute(select(GeoCity).where(GeoCity.name == "Indore"))
        ).scalar_one()
        nashik = (
            await session.execute(select(GeoCity).where(GeoCity.name == "Nashik"))
        ).scalar_one()
        assert (indore.prefix, indore.city_code, indore.lgd_code) == (
            "MP09",
            "09",
            250901,
        )
        assert (nashik.prefix, nashik.city_code, nashik.lgd_code) == (
            "MH15",
            "15",
            251442,
        )
        await session.rollback()


def _write_seed(tmp_path: Path, states: str, districts: str, cities: str) -> Path:
    (tmp_path / "states.csv").write_text(states, encoding="utf-8")
    (tmp_path / "districts.csv").write_text(districts, encoding="utf-8")
    (tmp_path / "cities.csv").write_text(cities, encoding="utf-8")
    return tmp_path


_STATE_HEADER = "lgd_code,name,code,is_union_territory\n"
_DISTRICT_HEADER = "lgd_code,name,state_lgd_code\n"
_CITY_HEADER = (
    "lgd_code,name,state_lgd_code,district_lgd_code,district_name,"
    "ulb_type_code,census_2011_code,city_code\n"
)


def test_valid_seed_files_are_counted(tmp_path):
    data_dir = _write_seed(
        tmp_path,
        _STATE_HEADER + "23,Madhya Pradesh,MP,false\n",
        _DISTRICT_HEADER + "452,Indore,23\n",
        _CITY_HEADER + "250901,Indore,23,452,Indore,4,,09\n",
    )
    counts = validate_geo_files(data_dir)
    assert counts == {"states": 1, "districts": 1, "cities": 1}


def test_duplicate_city_code_is_rejected_before_any_write(tmp_path):
    """Two cities claiming ``MP09`` would collide with the Indore prefix."""
    data_dir = _write_seed(
        tmp_path,
        _STATE_HEADER + "23,Madhya Pradesh,MP,false\n",
        _DISTRICT_HEADER + "452,Indore,23\n",
        _CITY_HEADER
        + "250901,Indore,23,452,Indore,4,,09\n"
        + "250902,Impostor,23,452,Indore,4,,09\n",
    )
    with pytest.raises(ValueError, match="collision"):
        validate_geo_files(data_dir)


def test_duplicate_lgd_code_is_rejected(tmp_path):
    data_dir = _write_seed(
        tmp_path,
        _STATE_HEADER + "23,Madhya Pradesh,MP,false\n",
        _DISTRICT_HEADER + "452,Indore,23\n",
        _CITY_HEADER
        + "250901,Indore,23,452,Indore,4,,09\n"
        + "250901,Duplicate,23,452,Indore,4,,10\n",
    )
    with pytest.raises(ValueError, match="Duplicate city LGD code"):
        validate_geo_files(data_dir)


def test_unknown_state_reference_is_rejected(tmp_path):
    data_dir = _write_seed(
        tmp_path,
        _STATE_HEADER + "23,Madhya Pradesh,MP,false\n",
        _DISTRICT_HEADER + "452,Indore,23\n",
        _CITY_HEADER + "250901,Indore,999,452,Indore,4,,09\n",
    )
    with pytest.raises(ValueError, match="unknown state LGD code"):
        validate_geo_files(data_dir)


def test_committed_seed_files_validate():
    counts = validate_geo_files(DATA_DIR)
    assert counts == {"states": 36, "districts": 784, "cities": 5051}
