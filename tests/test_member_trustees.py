"""The post-holder listing is derived from the member's role or designation.

A post holder is a community member whose linked login account carries
``GROUP_ADMIN`` or ``SUPER_ADMIN``, or whose group designation is a recognised
office (President, Vice President, Chairman, Protector, Secretary, Treasurer,
Publicity Secretary, ...). The member's own group decides which group's post
holder they are, so a SUPER_ADMIN is a post holder of the group they were added
to, not of every group. Unrelated job titles must not qualify.
"""

from __future__ import annotations

import time
from uuid import UUID

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


def _role_id(session: Session, role_name: str) -> UUID:
    return session.execute(
        select(Role.id).where(Role.name == role_name)
    ).scalar_one()


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
            user = User(
                phone_number=phone,
                password_hash=hash_password(PASSWORD),
                full_name="Trustee Listing Admin",
                is_email_verified=True,
                is_phone_verified=True,
                role_id=_role_id(session, "SUPER_ADMIN"),
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


def _create_group(client: TestClient, auth: dict) -> str:
    response = client.post(
        "/api/v1/groups",
        json={
            "name": f"Trustee Group {str(time.time_ns())[-8:]}",
            "state": "Madhya Pradesh",
            "city": "Indore",
        },
        headers=auth,
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["data"]["id"]


def _create_user(role_name: str, group_id: str | None = None) -> str:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        with Session(engine) as session:
            user = User(
                phone_number=_next_phone(),
                password_hash=hash_password(PASSWORD),
                full_name=f"Trustee {role_name} {str(time.time_ns())[-6:]}",
                is_email_verified=True,
                is_phone_verified=True,
                role_id=_role_id(session, role_name),
                group_id=UUID(group_id) if group_id else None,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return str(user.id)
    finally:
        engine.dispose()


def _set_user_role(user_id: str, role_name: str) -> None:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        with Session(engine) as session:
            user = session.get(User, UUID(user_id))
            assert user is not None
            user.role_id = _role_id(session, role_name)
            session.commit()
    finally:
        engine.dispose()


def _create_member(
    client: TestClient, auth: dict, *, user_id: str | None = None, **fields
) -> dict:
    payload = {
        "full_name": f"Trustee Case {str(time.time_ns())[-6:]}",
        "mobile_number": _next_phone(),
    }
    if user_id is not None:
        payload["user_id"] = user_id
    payload.update(fields)
    response = client.post("/api/v1/members", json=payload, headers=auth)
    assert response.status_code in (200, 201), response.text
    return response.json()["data"]


def _create_role_member(
    client: TestClient,
    auth: dict,
    role_name: str,
    *,
    group_id: str | None = None,
) -> dict:
    user_id = _create_user(role_name, group_id=group_id)
    return _create_member(client, auth, user_id=user_id, group_id=group_id)


def _trustee_items(client: TestClient, auth: dict, **params) -> dict[str, dict]:
    response = client.get(
        "/api/v1/trustees/directory",
        params={"page_size": 100, **params},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()["data"]}


def _trustee_ids(client: TestClient, auth: dict, **params) -> set[str]:
    return set(_trustee_items(client, auth, **params))


def _member_ids(client: TestClient, auth: dict) -> set[str]:
    response = client.get("/api/v1/members", params={"page_size": 100}, headers=auth)
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["data"]}


def test_a_group_admin_member_is_a_trustee(client: TestClient, auth: dict):
    group_id = _create_group(client, auth)
    member = _create_role_member(client, auth, "GROUP_ADMIN", group_id=group_id)

    items = _trustee_items(client, auth)
    assert member["id"] in items
    assert items[member["id"]]["role"] == "GROUP_ADMIN"


def test_a_super_admin_member_is_a_trustee_of_its_group(
    client: TestClient, auth: dict
):
    group_id = _create_group(client, auth)
    member = _create_role_member(client, auth, "SUPER_ADMIN", group_id=group_id)

    items = _trustee_items(client, auth)
    assert member["id"] in items
    assert items[member["id"]]["role"] == "SUPER_ADMIN"
    assert items[member["id"]]["group_id"] == group_id


POST_HOLDER_DESIGNATIONS = (
    "President",
    "Vice President",
    "Chairman",
    "Chairman & Managing Director",
    "Protector",
    "Secretary",
    "Treasurer",
    "Publicity Secretary",
    "Secretary Treasurer",
)


@pytest.mark.parametrize("designation", POST_HOLDER_DESIGNATIONS)
def test_a_member_with_a_post_holder_designation_is_a_trustee(
    client: TestClient, auth: dict, designation: str
):
    group_id = _create_group(client, auth)
    member = _create_role_member(
        client,
        auth,
        "MEMBER",
        group_id=group_id,
    )
    client.patch(
        f"/api/v1/members/{member['id']}",
        json={"group_designation": designation},
        headers=auth,
    )

    items = _trustee_items(client, auth)
    assert member["id"] in items
    assert items[member["id"]]["designation"] == designation
    assert items[member["id"]]["role"] is None


@pytest.mark.parametrize("designation", ("president", "CHAIRMAN", "treasurer"))
def test_post_holder_designation_matching_is_case_insensitive(
    client: TestClient, auth: dict, designation: str
):
    member = _create_member(client, auth, group_designation=designation)
    assert member["id"] in _trustee_ids(client, auth)


@pytest.mark.parametrize(
    "designation",
    (
        "Company Secretary",
        "Past President",
        "First Secretary",
        "Executive President",
        "Board Member",
        "Volunteer",
    ),
)
def test_unrelated_designations_are_not_post_holders(
    client: TestClient, auth: dict, designation: str
):
    member = _create_member(client, auth, group_designation=designation)
    assert member["id"] not in _trustee_ids(client, auth)


def test_a_member_without_an_account_can_be_a_post_holder(
    client: TestClient, auth: dict
):
    member = _create_member(client, auth, group_designation="President")
    assert member["id"] in _trustee_ids(client, auth)


def test_a_trustee_is_also_a_member(client: TestClient, auth: dict):
    group_id = _create_group(client, auth)
    member = _create_role_member(client, auth, "GROUP_ADMIN", group_id=group_id)

    assert member["id"] in _trustee_ids(client, auth)
    assert member["id"] in _member_ids(client, auth)


def test_the_role_filter_narrows_the_listing(client: TestClient, auth: dict):
    group_admin = _create_role_member(
        client, auth, "GROUP_ADMIN", group_id=_create_group(client, auth)
    )
    super_admin = _create_role_member(
        client, auth, "SUPER_ADMIN", group_id=_create_group(client, auth)
    )
    designated = _create_member(client, auth, group_designation="President")

    ids = _trustee_ids(client, auth, role="SUPER_ADMIN")
    assert super_admin["id"] in ids
    assert group_admin["id"] not in ids
    # An explicit role filter stays role-only.
    assert designated["id"] not in ids


def test_the_role_filter_rejects_an_unknown_role(client: TestClient, auth: dict):
    response = client.get(
        "/api/v1/trustees", params={"role": "MEMBER"}, headers=auth
    )
    assert response.status_code == 400, response.text


def test_promoting_a_member_makes_it_a_trustee(client: TestClient, auth: dict):
    group_id = _create_group(client, auth)
    user_id = _create_user("MEMBER", group_id=group_id)
    member = _create_member(client, auth, user_id=user_id, group_id=group_id)
    assert member["id"] not in _trustee_ids(client, auth)

    _set_user_role(user_id, "GROUP_ADMIN")
    assert member["id"] in _trustee_ids(client, auth)

    _set_user_role(user_id, "MEMBER")
    assert member["id"] not in _trustee_ids(client, auth)


def test_the_trustee_listing_carries_the_member_and_the_role(
    client: TestClient, auth: dict
):
    group_id = _create_group(client, auth)
    member = _create_role_member(client, auth, "GROUP_ADMIN", group_id=group_id)

    item = _trustee_items(client, auth)[member["id"]]
    # The trustee is the member: the same id, not a second record.
    assert item["member_id"] == member["id"]
    assert item["role"] == "GROUP_ADMIN"
    assert item["full_name"]


def test_the_trustee_listing_is_paginated(client: TestClient, auth: dict):
    _create_role_member(
        client, auth, "GROUP_ADMIN", group_id=_create_group(client, auth)
    )
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
    group_id = _create_group(client, auth)
    president = _create_role_member(
        client, auth, "GROUP_ADMIN", group_id=group_id
    )
    secretary = _create_role_member(
        client, auth, "SUPER_ADMIN", group_id=group_id
    )
    client.patch(
        f"/api/v1/members/{president['id']}",
        json={"group_designation": "President"},
        headers=auth,
    )
    client.patch(
        f"/api/v1/members/{secretary['id']}",
        json={"group_designation": "Secretary"},
        headers=auth,
    )

    ids = _trustee_ids(client, auth, designation="president")
    assert president["id"] in ids
    assert secretary["id"] not in ids


def test_the_member_listing_can_be_narrowed_to_the_trustees(
    client: TestClient, auth: dict
):
    group_id = _create_group(client, auth)
    trustee = _create_role_member(client, auth, "GROUP_ADMIN", group_id=group_id)
    designated = _create_member(client, auth, group_designation="Secretary")
    plain = _create_member(client, auth)
    assert plain["id"] not in _trustee_ids(client, auth)

    response = client.get(
        "/api/v1/members",
        params={"trustee_only": "true", "page_size": 100},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["data"]}
    assert trustee["id"] in ids
    assert designated["id"] in ids
    assert plain["id"] not in ids


def test_the_member_listing_search_keeps_the_trustee_filter(
    client: TestClient, auth: dict
):
    group_id = _create_group(client, auth)
    trustee = _create_role_member(client, auth, "GROUP_ADMIN", group_id=group_id)
    plain = _create_member(client, auth)
    assert plain["id"] not in _trustee_ids(client, auth)

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
    group_id = _create_group(client, auth)
    member = _create_role_member(client, auth, "GROUP_ADMIN", group_id=group_id)

    response = client.get("/api/v1/trustees", params={"page_size": 100}, headers=auth)
    assert response.status_code == 200, response.text
    items = response.json()["data"]
    ids = {item["id"] for item in items}
    assert member["id"] in ids
    for item in items:
        # Every post holder is a member and qualifies through an administrator
        # role or a recognised post-holder designation.
        assert item["member_id"] == item["id"]
        assert item["role"] in ("GROUP_ADMIN", "SUPER_ADMIN", None)
        assert item["role"] or item["designation"]


def test_the_board_seats_keep_their_own_listing(client: TestClient, auth: dict):
    """The seats of the board are their own records and have their own listing."""
    response = client.get(
        "/api/v1/trustees/board", params={"page_size": 100}, headers=auth
    )
    assert response.status_code == 200, response.text
    board = response.json()["data"]
    for item in board:
        assert "designation" in item
        assert "first_name" in item
    assert isinstance(board, list)


def test_a_board_seat_with_a_post_holder_designation_makes_a_member_a_trustee(
    client: TestClient, auth: dict
):
    """A seat mirrors its post-holder designation onto the member."""
    group_id = _create_group(client, auth)
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

    board = client.get(
        "/api/v1/trustees/board", params={"page_size": 100}, headers=auth
    ).json()["data"]
    assert seat["id"] in {item["id"] for item in board}
    # The seat's designation is mirrored onto the member, so the member now
    # qualifies as a post holder even without an administrator role.
    items = _trustee_items(client, auth)
    assert member["id"] in items
    assert items[member["id"]]["designation"] == "Treasurer"
    assert items[member["id"]]["role"] is None


def test_a_trustee_directory_item_is_never_a_duplicate_member(
    client: TestClient, auth: dict
):
    """One member is one trustee: the directory adds no rows of its own."""
    group_id = _create_group(client, auth)
    member = _create_role_member(client, auth, "GROUP_ADMIN", group_id=group_id)

    items = _trustee_items(client, auth)
    matching = [item for item in items.values() if item["id"] == member["id"]]
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

        admin = User(
            phone_number=phone,
            password_hash=hash_password(PASSWORD),
            full_name="Trustee Group Admin",
            is_email_verified=True,
            is_phone_verified=True,
            role_id=_role_id(session, "GROUP_ADMIN"),
            group_id=group_a.id,
        )
        foreign = User(
            phone_number=_next_phone(),
            password_hash=hash_password(PASSWORD),
            full_name="Foreign Trustee",
            is_email_verified=True,
            is_phone_verified=True,
            role_id=_role_id(session, "SUPER_ADMIN"),
        )
        session.add_all([admin, foreign])
        session.commit()
        session.refresh(admin)
        session.refresh(foreign)

        member_a = Member(
            user_id=admin.id,
            group_id=group_a.id,
            first_name="Local",
            last_name="Trustee",
        )
        member_b = Member(
            user_id=foreign.id,
            group_id=group_b.id,
            first_name="Foreign",
            last_name="Trustee",
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

        ids = _trustee_ids(client, headers)
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
            session.execute(
                delete(User).where(User.id.in_([admin.id, foreign.id]))
            )
            session.execute(
                delete(SocialGroup).where(SocialGroup.id.in_([group_a_id, group_b_id]))
            )
            session.commit()
        engine.dispose()
