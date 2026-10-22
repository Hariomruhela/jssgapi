"""Member IDs: ``MP09001`` + per-group sequence -> ``MP09001001``.

Adding a member to a group mints ``members.membership_number`` from that
group's own counter (``member_id_counters``), the same atomic upsert that
backs Group IDs. These tests pin down the contract for *every* state/city:
creation with a group, adding through ``POST /groups/{id}/members``, moves
between groups, idempotent re-adds, removal, client-supplied values being
ignored, and uniqueness under concurrency.
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models.group import MemberIdCounter, SocialGroup
from app.models.member import Member
from app.models.user import Role, User
from app.services.group_id_service import (
    format_member_id,
    membership_number_matches,
    next_member_id,
)

PASSWORD = "StrongPass123!"

INDORE = {"state": "Madhya Pradesh", "city": "Indore"}
NASHIK = {"state": "Maharashtra", "city": "Nashik"}

_created_members: list[str] = []
_created_groups: list[str] = []
_created_group_ids: list[str] = []


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


def _next_phone() -> str:
    return f"+919{str(time.time_ns())[-9:]}"


@pytest.fixture(scope="module")
def _engine():
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        yield engine
    finally:
        engine.dispose()


@asynccontextmanager
async def _isolated_session() -> AsyncIterator[AsyncSession]:
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
        return {"uid": f"memberids-{id_token}", "phone_number": id_token}

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
                    full_name="Member ID Admin",
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
    with Session(_engine) as session:
        if _created_members:
            session.execute(
                delete(Member).where(Member.id.in_(_created_members))
            )
        if _created_groups:
            session.execute(
                delete(SocialGroup).where(SocialGroup.id.in_(_created_groups))
            )
        if _created_group_ids:
            session.execute(
                delete(MemberIdCounter).where(
                    MemberIdCounter.group_id.in_(_created_group_ids)
                )
            )
        session.commit()


# --- helpers ----------------------------------------------------------------


def _create_group(client: TestClient, auth: dict, name: str, **place) -> dict:
    payload = {"name": name, **(place or INDORE)}
    response = client.post("/api/v1/groups", json=payload, headers=auth)
    assert response.status_code in (200, 201), response.text
    group = response.json()["data"]
    _created_groups.append(group["id"])
    if group.get("group_id"):
        _created_group_ids.append(group["group_id"])
    return group


def _create_member(
    client: TestClient, auth: dict, group_id: str | None = None, **fields
) -> dict:
    payload = {
        "full_name": f"Member {uuid4().hex[:8]}",
        "mobile_number": _next_phone(),
        **fields,
    }
    if group_id is not None:
        payload["group_id"] = group_id
    response = client.post("/api/v1/members", json=payload, headers=auth)
    assert response.status_code in (200, 201), response.text
    member = response.json()["data"]
    _created_members.append(member["id"])
    return member


def _add_to_group(client: TestClient, auth: dict, group: dict, member: dict):
    response = client.post(
        f"/api/v1/groups/{group['id']}/members",
        json={"member_id": member["id"]},
        headers=auth,
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["data"]


def _assert_member_id(value: str | None, group_id: str) -> str:
    assert value, f"expected a member ID for {group_id}"
    assert re.fullmatch(re.escape(group_id) + r"\d{3,}", value), value
    return value


# --- shape & ownership ------------------------------------------------------


def test_member_created_with_group_gets_its_member_id(client, auth):
    group = _create_group(client, auth, f"Member ID Shape {uuid4().hex[:8]}")

    member = _create_member(client, auth, group_id=group["id"])

    number = _assert_member_id(member["membership_number"], group["group_id"])
    assert number == group["group_id"] + "001"


def test_client_sent_membership_number_is_ignored_for_grouped_member(client, auth):
    group = _create_group(client, auth, f"Member ID Spoof {uuid4().hex[:8]}")

    member = _create_member(
        client,
        auth,
        group_id=group["id"],
        membership_number="FAKE-0001",
    )

    number = _assert_member_id(member["membership_number"], group["group_id"])
    assert number != "FAKE-0001"


def test_member_without_group_has_no_member_id(client, auth):
    member = _create_member(client, auth)

    assert member["membership_number"] is None


def test_member_id_works_for_any_state_and_city(client, auth):
    """Not just MP/Indore: the counter follows the group's own ID everywhere."""
    nashik = _create_group(client, auth, f"Nashik Members {uuid4().hex[:8]}", **NASHIK)

    member = _create_member(client, auth, group_id=nashik["id"])

    _assert_member_id(member["membership_number"], nashik["group_id"])
    assert nashik["group_id"].startswith("MH15")


# --- adding through POST /groups/{id}/members --------------------------------


def test_members_added_to_a_group_sequence_from_001(client, auth):
    group = _create_group(client, auth, f"Member Sequence {uuid4().hex[:8]}")
    first = _create_member(client, auth)
    second = _create_member(client, auth)

    one = _add_to_group(client, auth, group, first)
    two = _add_to_group(client, auth, group, second)

    assert one["membership_number"] == group["group_id"] + "001"
    assert two["membership_number"] == group["group_id"] + "002"


def test_adding_the_same_member_twice_keeps_the_same_id(client, auth):
    group = _create_group(client, auth, f"Idempotent Add {uuid4().hex[:8]}")
    member = _create_member(client, auth)

    first = _add_to_group(client, auth, group, member)
    second = _add_to_group(client, auth, group, member)

    assert first["membership_number"] == second["membership_number"]
    assert second["membership_number"] == group["group_id"] + "001"


def test_removing_and_readding_a_member_keeps_their_id(client, auth):
    group = _create_group(client, auth, f"Readd Member {uuid4().hex[:8]}")
    member = _create_member(client, auth)
    added = _add_to_group(client, auth, group, member)
    number = added["membership_number"]

    removed = client.delete(
        f"/api/v1/groups/{group['id']}/members/{member['id']}", headers=auth
    )
    assert removed.status_code == 200, removed.text
    readded = _add_to_group(client, auth, group, member)
    assert readded["membership_number"] == number


def test_moving_a_member_between_groups_issues_a_new_id(client, auth):
    group_a = _create_group(client, auth, f"Move A {uuid4().hex[:8]}")
    group_b = _create_group(client, auth, f"Move B {uuid4().hex[:8]}", **NASHIK)
    member = _create_member(client, auth, group_id=group_a["id"])
    first_number = member["membership_number"]
    _assert_member_id(first_number, group_a["group_id"])

    moved = _add_to_group(client, auth, group_b, member)
    second_number = _assert_member_id(
        moved["membership_number"], group_b["group_id"]
    )
    assert second_number != first_number

    # Back to the original group: a fresh number from that counter, never the
    # old one recycled out of the pool.
    moved_back = _add_to_group(client, auth, group_a, member)
    third_number = _assert_member_id(
        moved_back["membership_number"], group_a["group_id"]
    )
    assert third_number != first_number


def test_each_group_counts_its_members_independently(client, auth):
    stamp = uuid4().hex[:8]
    group_a = _create_group(client, auth, f"Independent A {stamp}")
    group_b = _create_group(client, auth, f"Independent B {stamp}", **NASHIK)
    member_a = _create_member(client, auth, group_id=group_a["id"])
    member_b = _create_member(client, auth, group_id=group_b["id"])

    assert member_a["membership_number"] == group_a["group_id"] + "001"
    assert member_b["membership_number"] == group_b["group_id"] + "001"


def test_concurrent_member_adds_never_share_a_member_id(client, auth):
    group = _create_group(client, auth, f"Concurrent Members {uuid4().hex[:8]}")
    members = [_create_member(client, auth) for _ in range(5)]

    def _add(member: dict) -> dict:
        response = client.post(
            f"/api/v1/groups/{group['id']}/members",
            json={"member_id": member["id"]},
            headers=auth,
        )
        assert response.status_code in (200, 201), response.text
        return response.json()["data"]

    with ThreadPoolExecutor(max_workers=len(members)) as pool:
        added = list(pool.map(_add, members))

    numbers = [item["membership_number"] for item in added]
    assert len(set(numbers)) == len(numbers), numbers
    assert set(numbers) == {
        f"{group['group_id']}{seq:03d}" for seq in range(1, len(members) + 1)
    }


# --- pure formatting / matching ---------------------------------------------


def test_member_id_format_grows_past_three_digits():
    assert format_member_id("MP09001", 1) == "MP09001001"
    assert format_member_id("MP09001", 999) == "MP09001999"
    assert format_member_id("MP09001", 1000) == "MP090011000"


def test_membership_number_matches_only_its_own_group():
    assert membership_number_matches("MP09001001", "MP09001")
    assert not membership_number_matches(None, "MP09001")
    assert not membership_number_matches("MP09001001", "MH15002")
    assert not membership_number_matches("MP09001777", "MP0900100")
    # Sequence part must be digits only and at least SEQUENCE_WIDTH long.
    assert not membership_number_matches("MP0900112", "MP09001")
    assert not membership_number_matches("SEARCH-A-1", "MP09001")


async def test_rolled_back_add_returns_its_member_id_to_the_pool():
    async with _isolated_session() as session:
        first = await next_member_id(session, "MP09999")
        await session.rollback()
    async with _isolated_session() as session:
        again = await next_member_id(session, "MP09999")
        await session.rollback()
    assert again == first
