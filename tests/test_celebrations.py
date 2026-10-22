"""Today's birthdays and anniversaries.

The endpoint is read-only and scoped like the member directory. Each test picks
a birth/anniversary date of "today" or a fixed day and asserts that the matching
member surfaces (or does not) with both people and their photos.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models.member import Member
from app.models.user import Role, User

PASSWORD = "StrongPass123!"

#: Every member this module creates; removed at module teardown so later
#: modules (e.g. the Drive photo migration, which assumes an empty members
#: table) never see them.
_created_member_ids: list[str] = []


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
        return {"uid": f"celebrations-{id_token}", "phone_number": id_token}

    with mock.patch(
        "app.services.auth_service.firebase_verify_id_token", _fake_verify_id_token
    ):
        yield


@pytest.fixture(scope="module")
def auth(client: TestClient, _mock_firebase) -> dict:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    phone = _next_phone()
    try:
        with Session(engine) as session:
            role = session.execute(
                select(Role).where(Role.name == "SUPER_ADMIN")
            ).scalar_one()
            session.add(
                User(
                    phone_number=phone,
                    password_hash=hash_password(PASSWORD),
                    full_name="Celebrations Admin",
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


def _create_member(client: TestClient, auth: dict, **fields) -> dict:
    payload = {
        "full_name": f"Celebration {str(time.time_ns())[-6:]}",
        "mobile_number": _next_phone(),
        **fields,
    }
    response = client.post("/api/v1/members", json=payload, headers=auth)
    assert response.status_code in (200, 201), response.text
    member = response.json()["data"]
    _created_member_ids.append(member["id"])
    return member


@pytest.fixture(scope="module", autouse=True)
def _cleanup_created_members():
    yield
    if not _created_member_ids:
        return
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        with Session(engine) as session:
            member_ids = [UUID(member_id) for member_id in _created_member_ids]
            session.execute(delete(Member).where(Member.id.in_(member_ids)))
            session.commit()
    finally:
        engine.dispose()


def _member_ids(items: list[dict]) -> set[str]:
    return {item["member"]["id"] for item in items if item.get("member", {}).get("id")}


def test_member_birthday_today_is_listed(client: TestClient, auth: dict):
    today = date.today()
    member = _create_member(
        client,
        auth,
        date_of_birth=today.isoformat(),
        profile_photo_url="https://cdn.test/m.jpg",
    )
    response = client.get("/api/v1/celebrations/birthdays", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["message"] == "Today's birthdays fetched successfully"
    assert member["id"] in _member_ids(body["data"])
    item = next(i for i in body["data"] if i["member"]["id"] == member["id"])
    assert item["occasion"] == "member_birthday"
    assert item["member"]["date_of_birth"] == today.isoformat()


def test_spouse_birthday_today_is_listed(client: TestClient, auth: dict):
    today = date.today()
    member = _create_member(
        client,
        auth,
        spouse_name="Spouse Celebration",
        spouse_dob=today.isoformat(),
        spouse_photo_link="https://cdn.test/spouse.jpg",
    )
    response = client.get("/api/v1/celebrations/birthdays", headers=auth)
    assert response.status_code == 200, response.text
    item = next(
        (i for i in response.json()["data"] if i["member"]["id"] == member["id"]), None
    )
    assert item is not None
    assert item["occasion"] == "spouse_birthday"
    assert item["spouse"]["full_name"] == "Spouse Celebration"
    assert item["spouse"]["photo_url"] == "https://cdn.test/spouse.jpg"


def test_other_day_birthday_is_not_listed(client: TestClient, auth: dict):
    tomorrow = date.today() + timedelta(days=1)
    member = _create_member(client, auth, date_of_birth=tomorrow.isoformat())
    response = client.get("/api/v1/celebrations/birthdays", headers=auth)
    assert response.status_code == 200, response.text
    assert member["id"] not in _member_ids(response.json()["data"])


def test_anniversary_today_is_listed_with_group_and_location(
    client: TestClient, auth: dict
):
    today = date.today()
    member = _create_member(
        client,
        auth,
        spouse_name="Anniversary Spouse",
        anniversary_date=f"{today.year - 7}-{today.month:02d}-{today.day:02d}",
    )
    response = client.get("/api/v1/celebrations/anniversaries", headers=auth)
    assert response.status_code == 200, response.text
    item = next(
        (i for i in response.json()["data"] if i["member"]["id"] == member["id"]), None
    )
    assert item is not None
    assert item["occasion"] == "anniversary"
    assert item["years"] == 7
    assert item["spouse"]["full_name"] == "Anniversary Spouse"
    assert "group_name" in item and "location" in item


def test_anniversaries_endpoint_only_returns_anniversaries(
    client: TestClient, auth: dict
):
    today = date.today()
    # A birthday member (no anniversary) must not appear on the anniversary feed.
    member = _create_member(client, auth, date_of_birth=today.isoformat())
    response = client.get("/api/v1/celebrations/anniversaries", headers=auth)
    assert response.status_code == 200, response.text
    assert member["id"] not in _member_ids(response.json()["data"])


def test_birthday_matches_non_iso_date_formats(client: TestClient, auth: dict):
    """The sheet stores US ``M/D/YYYY`` and Indian ``DD-MM-YYYY`` dates too.

    These two spellings used to be dropped by the SQL prefilter, so a member
    whose (spouse's) birthday fell on them never surfaced.
    """
    today = date.today()
    us_member = _create_member(
        client,
        auth,
        spouse_name="US Format Spouse",
        spouse_dob=f"{today.month}/{today.day}/1980",
    )
    in_member = _create_member(
        client,
        auth,
        spouse_name="IN Format Spouse",
        anniversary_date=f"{today.day:02d}-{today.month:02d}-2010",
    )

    birthdays = client.get("/api/v1/celebrations/birthdays", headers=auth).json()
    assert us_member["id"] in _member_ids(birthdays["data"])

    anniversaries = client.get(
        "/api/v1/celebrations/anniversaries", headers=auth
    ).json()
    assert in_member["id"] in _member_ids(anniversaries["data"])
    item = next(
        i for i in anniversaries["data"] if i["member"]["id"] == in_member["id"]
    )
    assert item["years"] == today.year - 2010
