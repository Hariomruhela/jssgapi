"""The trustee listing is derived from the members' group designation.

A trustee is a community member whose ``group_designation`` carries a
designation: a member without one is a normal member and never shows up in the
trustee listing, and a member that gains or loses its designation appears in or
disappears from that listing without a second record being written.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models.group import SocialGroup
from app.models.member import Member
from app.models.user import Role, User

PASSWORD = "StrongPass123!"


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


def _next_phone() -> str:
    return f"+916{str(time.time_ns())[-9:]}"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def super_admin(client: TestClient) -> str:
    """A SUPER_ADMIN token, minted through the real login endpoint."""
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    phone = _next_phone()
    try:
        with Session(engine) as session:
            role = session.execute(
                select(Role).where(Role.name == "SUPER_ADMIN")
            ).scalar_one()
            user = User(
                phone_number=phone,
                password_hash=hash_password(PASSWORD),
                full_name="Trustee Listing Admin",
                is_email_verified=True,
                is_phone_verified=True,
                role_id=role.id,
            )
            session.add(user)
            session.commit()

        response = client.post(
            "/api/v1/auth/login",
            json={"phone_number": phone, "password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        yield response.json()["tokens"]["access_token"]
    finally:
        with Session(engine) as session:
            session.execute(delete(User).where(User.phone_number == phone))
            session.commit()
        engine.dispose()


@pytest.fixture(scope="module")
def auth(super_admin: str) -> dict:
    return {"Authorization": f"Bearer {super_admin}"}


def _create_member(client: TestClient, auth: dict, **fields) -> dict:
    payload = {
        "full_name": f"Trustee Case {str(time.time_ns())[-6:]}",
        "mobile_number": _next_phone(),
    }
    payload.update(fields)
    response = client.post("/api/v1/members", json=payload, headers=auth)
    assert response.status_code in (200, 201), response.text
    return response.json()["data"]


def _trustee_ids(client: TestClient, auth: dict) -> set[str]:
    response = client.get(
        "/api/v1/trustees/directory", params={"page_size": 100}, headers=auth
    )
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["data"]}


def _member_ids(client: TestClient, auth: dict) -> set[str]:
    response = client.get("/api/v1/members", params={"page_size": 100}, headers=auth)
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["data"]}


def test_a_member_with_a_designation_is_a_trustee(client: TestClient, auth: dict):
    member = _create_member(client, auth, group_designation="President")
    assert member["id"] in _trustee_ids(client, auth)


def test_a_member_without_a_designation_is_not_a_trustee(
    client: TestClient, auth: dict
):
    member = _create_member(client, auth)
    assert member["group_designation"] is None
    assert member["id"] not in _trustee_ids(client, auth)


def test_an_empty_designation_is_not_a_trustee(client: TestClient, auth: dict):
    member = _create_member(client, auth, group_designation="")
    assert member["id"] not in _trustee_ids(client, auth)


def test_a_blank_designation_is_not_a_trustee(client: TestClient, auth: dict):
    """Padding left behind by a sheet import must not make a member a trustee."""
    member = _create_member(client, auth, group_designation="   ")
    assert member["id"] not in _trustee_ids(client, auth)


def test_a_normal_member_stays_in_the_member_listing(client: TestClient, auth: dict):
    """The trustee listing narrows the member listing, it does not replace it."""
    trustee = _create_member(client, auth, group_designation="Secretary")
    plain = _create_member(client, auth)

    member_ids = _member_ids(client, auth)
    assert trustee["id"] in member_ids
    assert plain["id"] in member_ids


def test_adding_a_designation_turns_a_member_into_a_trustee(
    client: TestClient, auth: dict
):
    member = _create_member(client, auth)
    assert member["id"] not in _trustee_ids(client, auth)

    response = client.patch(
        f"/api/v1/members/{member['id']}",
        json={"group_designation": "Treasurer"},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    assert member["id"] in _trustee_ids(client, auth)


def test_removing_a_designation_turns_a_trustee_back_into_a_member(
    client: TestClient, auth: dict
):
    member = _create_member(client, auth, group_designation="President")
    assert member["id"] in _trustee_ids(client, auth)

    response = client.patch(
        f"/api/v1/members/{member['id']}",
        json={"group_designation": None},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    assert member["id"] not in _trustee_ids(client, auth)
    # The member itself is untouched by the change.
    assert member["id"] in _member_ids(client, auth)


def test_the_trustee_listing_carries_the_designation_and_the_member(
    client: TestClient, auth: dict
):
    member = _create_member(client, auth, group_designation="President")
    response = client.get(
        "/api/v1/trustees/directory", params={"page_size": 100}, headers=auth
    )
    assert response.status_code == 200, response.text
    items = {item["id"]: item for item in response.json()["data"]}
    assert member["id"] in items
    item = items[member["id"]]
    assert item["designation"] == "President"
    # The trustee is the member: the same id, not a second record.
    assert item["member_id"] == member["id"]
    assert item["full_name"]


def test_the_trustee_listing_is_paginated(client: TestClient, auth: dict):
    response = client.get(
        "/api/v1/trustees/directory",
        params={"page": 1, "page_size": 1},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    pagination = body["pagination"]
    assert pagination["page"] == 1
    assert pagination["page_size"] == 1
    assert pagination["total_pages"] >= 1
    assert len(body["data"]) <= 1


def test_the_trustee_listing_can_be_narrowed_to_one_designation(
    client: TestClient, auth: dict
):
    president = _create_member(client, auth, group_designation="President")
    secretary = _create_member(client, auth, group_designation="Secretary")

    response = client.get(
        "/api/v1/trustees/directory",
        params={"designation": "president", "page_size": 100},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["data"]}
    assert president["id"] in ids
    assert secretary["id"] not in ids


def test_the_member_listing_can_be_narrowed_to_the_trustees(
    client: TestClient, auth: dict
):
    trustee = _create_member(client, auth, group_designation="President")
    plain = _create_member(client, auth)

    response = client.get(
        "/api/v1/members",
        params={"trustee_only": "true", "page_size": 100},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]
    ids = {item["id"] for item in items}
    assert trustee["id"] in ids
    assert plain["id"] not in ids
    # Every trustee carries a designation; a blank one is not a designation.
    for item in items:
        assert (item.get("group_designation") or "").strip()


def test_the_member_listing_search_keeps_the_trustee_filter(
    client: TestClient, auth: dict
):
    trustee = _create_member(client, auth, group_designation="President")
    plain = _create_member(client, auth)

    response = client.get(
        "/api/v1/members",
        params={
            "query": trustee["first_name"] or trustee["full_name"],
            "trustee_only": "true",
        },
        headers=auth,
    )
    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["data"]}
    assert trustee["id"] in ids
    assert plain["id"] not in ids


def test_the_trustee_listing_requires_authentication(client: TestClient):
    response = client.get("/api/v1/trustees/directory")
    assert response.status_code == 401, response.text


def test_the_trustee_listing_serves_the_members(client: TestClient, auth: dict):
    """``GET /trustees`` is the trustee listing: the members with a designation."""
    member = _create_member(client, auth, group_designation="President")
    response = client.get("/api/v1/trustees", params={"page_size": 100}, headers=auth)
    assert response.status_code == 200, response.text
    items = response.json()["data"]
    ids = {item["id"] for item in items}
    assert member["id"] in ids
    for item in items:
        # Every trustee is a member and carries a designation.
        assert item["member_id"] == item["id"]
        assert item["designation"].strip()


def test_the_board_seats_keep_their_own_listing(client: TestClient, auth: dict):
    """The seats of the board are their own records and have their own listing."""
    response = client.get(
        "/api/v1/trustees/board", params={"page_size": 100}, headers=auth
    )
    assert response.status_code == 200, response.text
    board = response.json()["data"]
    # A board seat is a trustee record, never a member row.
    for item in board:
        assert "designation" in item
        assert "first_name" in item
    assert isinstance(board, list)


def test_a_board_seat_puts_its_member_in_the_trustee_listing(
    client: TestClient, auth: dict
):
    """Promoting a member to the board gives it the seat's designation."""
    group = client.post(
        "/api/v1/groups",
        json={
            "name": f"Trustee Board {str(time.time_ns())[-6:]}",
            "state": "Madhya Pradesh",
            "city": "Indore",
        },
        headers=auth,
    )
    assert group.status_code in (200, 201), group.text
    group_id = group.json()["data"]["id"]

    member = _create_member(client, auth, group_id=group_id)
    assert member["id"] not in _trustee_ids(client, auth)

    response = client.post(
        "/api/v1/trustees",
        json={
            "group_id": group_id,
            "member_id": member["id"],
            "first_name": "",
            "last_name": "",
            "designation": "Treasurer",
        },
        headers=auth,
    )
    assert response.status_code in (200, 201), response.text
    seat = response.json()["data"]
    assert seat["member_id"] == member["id"]

    assert member["id"] in _trustee_ids(client, auth)
    listing = client.get(
        "/api/v1/trustees/directory", params={"page_size": 100}, headers=auth
    ).json()
    item = next(i for i in listing["data"] if i["id"] == member["id"])
    assert item["designation"] == "Treasurer"

    # Retiring the seat takes the designation with it.
    response = client.delete(f"/api/v1/trustees/{seat['id']}", headers=auth)
    assert response.status_code == 200, response.text
    assert member["id"] not in _trustee_ids(client, auth)


def test_a_trustee_directory_item_is_never_a_duplicate_member(
    client: TestClient, auth: dict
):
    """One member is one trustee: the directory adds no rows of its own."""
    member = _create_member(client, auth, group_designation="President")
    response = client.get(
        "/api/v1/trustees/directory", params={"page_size": 100}, headers=auth
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]
    matching = [item for item in items if item["id"] == member["id"]]
    assert len(matching) == 1
    assert matching[0]["member_id"] == member["id"]


def test_the_trustee_listing_is_group_scoped(client: TestClient):
    """A GROUP_ADMIN sees the trustees of its own group, and only those."""
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    stamp = str(time.time_ns())[-8:]
    group_a = SocialGroup(name=f"Trustee Group A {stamp}")
    group_b = SocialGroup(name=f"Trustee Group B {stamp}")
    phone = _next_phone()
    with Session(engine) as session:
        session.add_all([group_a, group_b])
        session.commit()
        session.refresh(group_a)
        session.refresh(group_b)

        role = session.execute(
            select(Role).where(Role.name == "GROUP_ADMIN")
        ).scalar_one()
        user = User(
            phone_number=phone,
            password_hash=hash_password(PASSWORD),
            full_name="Trustee Group Admin",
            is_email_verified=True,
            is_phone_verified=True,
            role_id=role.id,
            group_id=group_a.id,
        )
        session.add(user)
        session.commit()
        session.refresh(user)

        member_a = Member(
            user_id=user.id,
            group_id=group_a.id,
            first_name="Local",
            last_name="Trustee",
            profile_data={"group_designation": "President"},
        )
        member_b = Member(
            group_id=group_b.id,
            first_name="Foreign",
            last_name="Trustee",
            profile_data={"group_designation": "President"},
        )
        session.add_all([member_a, member_b])
        session.commit()
        session.refresh(member_a)
        session.refresh(member_b)
        member_a_id, member_b_id, group_a_id, group_b_id = (
            member_a.id,
            member_b.id,
            group_a.id,
            group_b.id,
        )

    try:
        login = client.post(
            "/api/v1/auth/login",
            json={"phone_number": phone, "password": PASSWORD},
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}

        response = client.get(
            "/api/v1/trustees/directory", params={"page_size": 100}, headers=headers
        )
        assert response.status_code == 200, response.text
        ids = {item["id"] for item in response.json()["data"]}
        assert str(member_a_id) in ids
        assert str(member_b_id) not in ids

        # Asking for another group is refused, not quietly narrowed.
        response = client.get(
            "/api/v1/trustees/directory",
            params={"group_id": str(group_b_id)},
            headers=headers,
        )
        assert response.status_code == 403, response.text
    finally:
        with Session(engine) as session:
            session.execute(
                delete(Member).where(Member.id.in_([member_a_id, member_b_id]))
            )
            session.execute(delete(User).where(User.id == user.id))
            session.execute(
                delete(SocialGroup).where(SocialGroup.id.in_([group_a_id, group_b_id]))
            )
            session.commit()
        engine.dispose()
