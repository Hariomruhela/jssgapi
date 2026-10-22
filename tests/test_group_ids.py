"""Automatic Group ID generation: validation, sequencing and safety.

``POST /api/v1/groups`` mints ``STATE_CODE + CITY_CODE + SEQUENCE`` IDs
(``MP09001`` in Indore, ``MH15002`` in Nashik) from a PostgreSQL counter, so
these tests pin down the whole contract: mandatory, geo-data-validated
state/city; per-city sequences that never collide under concurrency; IDs that
outlive updates; and a Google Sheet sync that keeps hands off.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models.geo import GeoCity
from app.models.group import GroupIdCounter, SocialGroup
from app.models.user import Role, User
from app.services.group_id_service import (
    format_group_id,
    next_group_id,
    resolve_city,
)
from app.services.sheet_groups_service import import_member_groups

PASSWORD = "StrongPass123!"

INDORE = {"state": "Madhya Pradesh", "city": "Indore"}
NASHIK = {"state": "Maharashtra", "city": "Nashik"}

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
    """A session on a connection of its own, opened in the test's event loop.

    The shared engine pools connections, and one opened by another loop (the
    ``TestClient`` portal's, an earlier test's) cannot be reused here. NullPool
    closes every connection with its loop, so this never borrows a stale one.
    """
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
    payload = {"name": name, **(place or INDORE)}
    response = client.post("/api/v1/groups", json=payload, headers=auth)
    assert response.status_code in (200, 201), response.text
    group = response.json()["data"]
    _created_groups.append(group["id"])
    return group


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client
        # Pooled connections belong to this portal's loop, which closes with
        # the context manager; the next module's TestClient would otherwise
        # check one out from a dead loop.
        assert test_client.portal is not None
        test_client.portal.call(_dispose_app_engine)


async def _dispose_app_engine() -> None:
    from app.database import engine

    await engine.dispose()


@pytest.fixture(scope="module")
def _mock_firebase():
    from unittest import mock

    def _fake_verify_id_token(id_token: str) -> dict:
        return {"uid": f"groupids-{id_token}", "phone_number": id_token}

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
                    full_name="Group ID Admin",
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


# --- ID shape ---------------------------------------------------------------


def test_indore_group_gets_the_next_mp09_id(client, auth):
    group = _create_group(client, auth, f"Indore Group {uuid4().hex[:8]}", **INDORE)

    assert re.fullmatch(r"MP09\d{3,}", group["group_id"]), group["group_id"]
    assert group["state"] == "Madhya Pradesh"
    assert group["city"] == "Indore"
    assert group["id"]


def test_nashik_group_gets_the_next_mh15_id(client, auth):
    group = _create_group(client, auth, f"Nashik Group {uuid4().hex[:8]}", **NASHIK)

    assert re.fullmatch(r"MH15\d{3,}", group["group_id"]), group["group_id"]
    assert group["state"] == "Maharashtra"
    assert group["city"] == "Nashik"


def test_state_and_city_are_stored_in_canonical_form(client, auth):
    """Sloppy input ("INDORE", "  madhya   pradesh ") still lands canonical."""
    group = _create_group(
        client,
        auth,
        f"Canon Group {uuid4().hex[:8]}",
        state="  madhya   pradesh ",
        city="INDORE",
    )
    assert group["group_id"].startswith("MP09")
    assert group["state"] == "Madhya Pradesh"
    assert group["city"] == "Indore"


def test_client_supplied_group_id_is_ignored(client, auth):
    name = f"Forged ID Group {uuid4().hex[:8]}"
    response = client.post(
        "/api/v1/groups",
        json={**INDORE, "name": name, "group_id": "ZZ99999"},
        headers=auth,
    )
    assert response.status_code in (200, 201), response.text
    group = response.json()["data"]
    _created_groups.append(group["id"])
    assert group["group_id"] != "ZZ99999"
    assert re.fullmatch(r"MP09\d{3,}", group["group_id"]), group["group_id"]


# --- validation -------------------------------------------------------------


def test_missing_state_is_rejected(client, auth):
    response = client.post(
        "/api/v1/groups",
        json={"name": f"No State {uuid4().hex[:8]}", "city": "Indore"},
        headers=auth,
    )
    assert response.status_code == 422, response.text


def test_missing_city_is_rejected(client, auth):
    response = client.post(
        "/api/v1/groups",
        json={"name": f"No City {uuid4().hex[:8]}", "state": "Madhya Pradesh"},
        headers=auth,
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "place",
    [
        {"state": "", "city": "Indore"},
        {"state": "   ", "city": "Indore"},
        {"state": "Madhya Pradesh", "city": ""},
        {"state": "Madhya Pradesh", "city": "  \t "},
    ],
    ids=["empty-state", "blank-state", "empty-city", "blank-city"],
)
def test_blank_state_or_city_is_rejected(client, auth, place):
    response = client.post(
        "/api/v1/groups",
        json={"name": f"Blank Place {uuid4().hex[:8]}", **place},
        headers=auth,
    )
    assert response.status_code == 422, response.text


def test_unconfigured_city_is_rejected(client, auth):
    """A city absent from the LGD geo data never mints an ID."""
    response = client.post(
        "/api/v1/groups",
        json={
            "name": f"Unsupported City {uuid4().hex[:8]}",
            "state": "Madhya Pradesh",
            "city": "Atlantis City",
        },
        headers=auth,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert "Atlantis City" in response.json()["message"]


def test_city_from_another_state_is_rejected(client, auth):
    """Nashik is configured - but for Maharashtra, not Madhya Pradesh."""
    response = client.post(
        "/api/v1/groups",
        json={"name": f"Wrong State {uuid4().hex[:8]}", **INDORE, "city": "Nashik"},
        headers=auth,
    )
    assert response.status_code == 422, response.text
    assert "Maharashtra" in response.json()["message"]


def test_unconfigured_state_is_rejected(client, auth):
    response = client.post(
        "/api/v1/groups",
        json={
            "name": f"Unknown State {uuid4().hex[:8]}",
            "state": "Kalinga",
            "city": "Indore",
        },
        headers=auth,
    )
    assert response.status_code == 422, response.text
    assert "Kalinga" in response.json()["message"]


# --- sequencing -------------------------------------------------------------


def test_same_city_sequences_forward(client, auth):
    stamp = uuid4().hex[:8]
    first = _create_group(client, auth, f"Seq A {stamp}", **INDORE)
    second = _create_group(client, auth, f"Seq B {stamp}", **INDORE)
    third = _create_group(client, auth, f"Seq C {stamp}", **INDORE)

    prefix = "MP09"
    sequences = [
        _sequence_of(group["group_id"], prefix)
        for group in (first, second, third)
    ]
    assert sequences[1] == sequences[0] + 1
    assert sequences[2] == sequences[1] + 1
    assert len({first["group_id"], second["group_id"], third["group_id"]}) == 3


def test_each_city_counts_independently(client, auth, _engine):
    """A group in Indore must not move Nashik's counter, and vice versa."""
    mp_before = _counter_value(_engine, "MP09")
    mh_before = _counter_value(_engine, "MH15")

    _create_group(client, auth, f"MP Counter {uuid4().hex[:8]}", **INDORE)
    assert _counter_value(_engine, "MP09") == mp_before + 1
    assert _counter_value(_engine, "MH15") == mh_before

    _create_group(client, auth, f"MH Counter {uuid4().hex[:8]}", **NASHIK)
    assert _counter_value(_engine, "MH15") == mh_before + 1
    assert _counter_value(_engine, "MP09") == mp_before + 1


def test_sequence_expands_past_three_digits_without_truncating():
    """``MP09999`` is followed by ``MP091000`` - never truncated or reused."""
    assert format_group_id("MP09", 1) == "MP09001"
    assert format_group_id("MP09", 999) == "MP09999"
    assert format_group_id("MP09", 1000) == "MP091000"
    assert format_group_id("MP09", 100000) == "MP09100000"


async def test_a_rolled_back_request_returns_its_number():
    """The counter lives inside the request transaction, so an aborted create
    gives its number back instead of leaving a hole."""
    async with _isolated_session() as session:
        city = await resolve_city(session, "Madhya Pradesh", "Indore")
        first = await next_group_id(session, city)
        await session.rollback()
    async with _isolated_session() as session:
        again = await next_group_id(session, city)
        await session.rollback()
    assert again == first


def test_concurrent_creates_never_share_a_group_id(client, auth):
    stamp = uuid4().hex[:8]
    names = [f"Concurrent {stamp} #{i}" for i in range(6)]

    def _create(name: str) -> dict:
        response = client.post(
            "/api/v1/groups", json={**INDORE, "name": name}, headers=auth
        )
        assert response.status_code in (200, 201), response.text
        return response.json()["data"]

    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        groups = list(pool.map(_create, names))

    ids = [group["group_id"] for group in groups]
    assert len(set(ids)) == len(names), ids
    sequences = sorted(_sequence_of(group_id, "MP09") for group_id in ids)
    assert sequences == list(range(sequences[0], sequences[0] + len(names))), sequences
    _created_groups.extend(group["id"] for group in groups)


# --- persistence guarantees -------------------------------------------------


def test_update_never_changes_the_generated_id(client, auth):
    stamp = uuid4().hex[:8]
    group = _create_group(client, auth, f"Stable ID {stamp}", **INDORE)
    original_id = group["group_id"]

    response = client.patch(
        f"/api/v1/groups/{group['id']}",
        json={
            "name": f"Stable ID Renamed {stamp}",
            "description": "edited after creation",
            # A hostile/stale client cannot rewrite the ID or the place.
            "group_id": "MH15999",
            "state": "Maharashtra",
            "city": "Nashik",
        },
        headers=auth,
    )
    assert response.status_code == 200, response.text
    updated = response.json()["data"]
    assert updated["group_id"] == original_id
    assert updated["state"] == "Madhya Pradesh"
    assert updated["city"] == "Indore"
    assert updated["description"] == "edited after creation"


def test_group_id_unique_constraint_is_enforced(_engine):
    """The unique index - not application code - is the last line of defence."""
    stamp = uuid4().hex[:8]
    taken = "ZZ00999"
    with Session(_engine) as session:
        first = SocialGroup(
            name=f"Unique A {stamp}", status="ACTIVE", is_active=True, group_id=taken
        )
        second = SocialGroup(
            name=f"Unique B {stamp}", status="ACTIVE", is_active=True, group_id=taken
        )
        session.add(first)
        session.flush()
        session.add(second)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_colliding_allocation_is_a_conflict_not_a_duplicate(client, auth, monkeypatch):
    """If the backstop ever fires the caller gets a 409, never a second row
    with the same ID - and the claimed sequence is rolled back with it."""
    stamp = uuid4().hex[:8]
    taken = _create_group(client, auth, f"Collision A {stamp}", **INDORE)

    async def _reuse_taken_id(session, city):
        return taken["group_id"]

    monkeypatch.setattr("app.api.v1.groups.next_group_id", _reuse_taken_id)
    response = client.post(
        "/api/v1/groups",
        json={**INDORE, "name": f"Collision B {stamp}"},
        headers=auth,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "CONFLICT"


async def test_repeated_sheet_sync_creates_no_duplicates_and_no_ids(_engine):
    """Sync materializes free-text names; it must not mint or rewrite IDs."""
    name = f"Sheet Sync Group {uuid4().hex[:8]}"

    async def _sync():
        async with _isolated_session() as session:
            summary = await import_member_groups(
                session,
                sheet_names=[name],
                include_member_data=False,
                link_members=False,
            )
            await session.commit()
            return summary

    first = await _sync()
    second = await _sync()
    assert name in first.created
    assert name not in second.created
    assert name in second.existing

    with Session(_engine) as session:
        rows = list(
            session.execute(
                select(SocialGroup).where(SocialGroup.name == name)
            ).scalars()
        )
        assert len(rows) == 1
        # No state/city came from the sheet, so no ID is guessed or backfilled.
        assert rows[0].group_id is None
        assert rows[0].state is None
        assert rows[0].city is None
        session.delete(rows[0])
        session.commit()


async def test_geo_prefixes_are_unique():
    """Every seeded city prefix (``MP09``, ``MH15``, ...) must be unique."""
    async with _isolated_session() as session:
        result = await session.execute(select(GeoCity.prefix))
        prefixes = list(result.scalars().all())
    assert len(prefixes) >= 5000, len(prefixes)
    assert len(prefixes) == len(set(prefixes))
