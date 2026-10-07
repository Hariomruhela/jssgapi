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
from app.models.user import Role, User
from app.services.profile_service import PROFILE_FIELDS

PASSWORD = "StrongPass123!"

PROFILE_FIELD_SET = set(PROFILE_FIELDS)
EXISTING_MEMBER_FIELDS = {
    "id",
    "user_id",
    "group_id",
    "first_name",
    "last_name",
    "membership_status",
    "user",
    "family_members",
    "professional_info",
    "created_at",
    "updated_at",
}


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
def _mock_firebase():
    from unittest import mock

    def _fake_verify_id_token(id_token: str) -> dict:
        return {"uid": f"members-{id_token}", "phone_number": id_token}

    with mock.patch(
        "app.services.auth_service.firebase_verify_id_token", _fake_verify_id_token
    ):
        yield


@pytest.fixture(scope="module")
def super_admin(client: TestClient, _mock_firebase) -> str:
    """A SUPER_ADMIN token.

    ``GET /members`` used to accept anonymous requests; it is now authenticated
    and group scoped, so these shape tests have to call it as an admin.
    """
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
                full_name="Members Shape Admin",
                is_email_verified=True,
                is_phone_verified=True,
                role_id=role.id,
            )
            session.add(user)
            session.commit()

        # Log in through the real endpoint rather than minting a token, so this
        # fixture also proves the login response stays usable for the panel.
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


@pytest.fixture(scope="module", autouse=True)
def _seed_member(client: TestClient, auth: dict) -> None:
    """One member so the shape and pagination assertions have a row to read.

    Without it these tests pass vacuously - or fail - on a fresh test database.
    """
    response = client.post(
        "/api/v1/members",
        json={"full_name": "Seed Member", "mobile_number": _next_phone()},
        headers=auth,
    )
    assert response.status_code in (200, 201), response.text


def _assert_items(items: list[dict]) -> None:
    for item in items:
        missing = PROFILE_FIELD_SET - item.keys()
        assert not missing, f"missing profile fields: {sorted(missing)}"
        assert EXISTING_MEMBER_FIELDS <= item.keys()
        for field in PROFILE_FIELD_SET:
            value = item[field]
            assert value is None or isinstance(
                value, str
            ), f"{field} is neither null nor a string: {value!r}"


def test_members_list_returns_every_profile_field(client: TestClient, auth: dict):
    response = client.get("/api/v1/members", params={"page_size": 100}, headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["message"] == "Members fetched successfully"
    assert body["pagination"]["page"] == 1

    items = body["data"]
    assert len(items) == min(body["pagination"]["total"], 100)
    _assert_items(items)


def test_members_list_search_returns_every_profile_field(
    client: TestClient, auth: dict
):
    listing = client.get("/api/v1/members", params={"page_size": 100}, headers=auth)
    assert listing.status_code == 200, listing.text
    sample = listing.json()["data"]
    if not sample:
        pytest.skip("no members in the database to search for")

    response = client.get(
        "/api/v1/members", params={"query": sample[0]["first_name"]}, headers=auth
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    # The search envelope deliberately omits `pagination`; the panel's Members
    # page already handles its absence.
    assert "pagination" not in body
    _assert_items(body["data"])


def test_members_list_keeps_pagination_envelope(client: TestClient, auth: dict):
    response = client.get(
        "/api/v1/members", params={"page": 1, "page_size": 2}, headers=auth
    )
    assert response.status_code == 200, response.text
    pagination = response.json()["pagination"]
    assert pagination["page"] == 1
    assert pagination["page_size"] == 2
    assert pagination["total_pages"] >= 1


def _create_member_in_group(client: TestClient, auth: dict, group_name: str) -> str:
    response = client.post(
        "/api/v1/members",
        json={
            "full_name": f"Group Filter {str(time.time_ns())[-6:]}",
            "mobile_number": _next_phone(),
            "social_group_name": group_name,
        },
        headers=auth,
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["data"]["id"]


def _ids(body: dict) -> list[str]:
    return [item["id"] for item in body["data"]]


def test_members_list_filters_by_social_group_name(client: TestClient, auth: dict):
    group_name = f"Filter Group {str(time.time_ns())[-6:]}"
    member_id = _create_member_in_group(client, auth, group_name)

    filtered = client.get(
        "/api/v1/members",
        params={"group_name": group_name, "page_size": 100},
        headers=auth,
    )
    assert filtered.status_code == 200, filtered.text
    assert member_id in _ids(filtered.json())

    # Case and padding differences must not change the result set.
    loose = client.get(
        "/api/v1/members",
        params={"group_name": f"  {group_name.upper()}  ", "page_size": 100},
        headers=auth,
    )
    assert loose.status_code == 200, loose.text
    assert member_id in _ids(loose.json())

    missing = client.get(
        "/api/v1/members",
        params={"group_name": f"Nobody {str(time.time_ns())[-6:]}", "page_size": 100},
        headers=auth,
    )
    assert missing.status_code == 200, missing.text
    assert member_id not in _ids(missing.json())

    # A blank filter is "no filter", not a group literally named "".
    blank = client.get(
        "/api/v1/members", params={"group_name": "   ", "page_size": 100}, headers=auth
    )
    assert blank.status_code == 200, blank.text
    assert blank.json()["pagination"]["total"] >= 1


def test_members_list_search_applies_the_group_name_filter(
    client: TestClient, auth: dict
):
    group_name = f"Search Group {str(time.time_ns())[-6:]}"
    member_id = _create_member_in_group(client, auth, group_name)

    response = client.get(
        "/api/v1/members",
        params={"query": "Group", "group_name": group_name, "page_size": 100},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    # The group filter must narrow the search too: every member sharing the
    # first name but sitting in another group stays out of the result.
    assert _ids(response.json()) == [member_id]


def test_members_social_groups_lists_distinct_names(
    client: TestClient, auth: dict
):
    group_name = f"Distinct Group {str(time.time_ns())[-6:]}"
    _create_member_in_group(client, auth, group_name)

    response = client.get("/api/v1/members/social-groups", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    names = body["data"]
    assert all(isinstance(name, str) for name in names)
    assert group_name in names
    assert len(names) == len(set(names))
    assert names == sorted(names, key=str.casefold)
