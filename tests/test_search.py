from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models.group import SocialGroup
from app.models.location import Location
from app.models.member import Member
from app.models.user import Role, User

PASSWORD = "StrongPass123!"


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


def _next_phone() -> str:
    return f"+916{str(int(time.time() * 1000))[-9:]}"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_user(engine, role_name: str, phone: str, group_id=None) -> User:
    with Session(engine) as session:
        role = session.execute(
            select(Role).where(Role.name == role_name)
        ).scalar_one()
        user = User(
            phone_number=phone,
            password_hash=hash_password(PASSWORD),
            full_name=f"Search {role_name}",
            is_email_verified=True,
            is_phone_verified=True,
            role_id=role.id,
            group_id=group_id,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _create_group(engine, name: str, location_id=None) -> SocialGroup:
    with Session(engine) as session:
        group = SocialGroup(name=name, location_id=location_id)
        session.add(group)
        session.commit()
        session.refresh(group)
        return group


def _create_location(engine, name: str, city: str) -> Location:
    with Session(engine) as session:
        location = Location(name=name, city=city, level="CITY")
        session.add(location)
        session.commit()
        session.refresh(location)
        return location


def _create_member(
    engine,
    user_id,
    group_id,
    first_name: str,
    last_name: str = "Member",
    membership_number: str | None = None,
    location_id=None,
) -> Member:
    with Session(engine) as session:
        member = Member(
            user_id=user_id,
            group_id=group_id,
            location_id=location_id,
            first_name=first_name,
            last_name=last_name,
            membership_number=membership_number,
        )
        session.add(member)
        session.commit()
        session.refresh(member)
        return member


def _login(client: TestClient, phone: str) -> str:
    resp = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["tokens"]["access_token"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module", autouse=True)
def _mock_firebase():
    from unittest import mock

    def _fake_verify_id_token(id_token: str) -> dict:
        return {"uid": f"search-{id_token}", "phone_number": id_token}

    with mock.patch(
        "app.services.auth_service.firebase_verify_id_token", _fake_verify_id_token
    ):
        yield


@pytest.fixture(scope="module")
def world(client):
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    marker = uuid.uuid4().hex[:8]

    location_a = _create_location(engine, f"SearchCityAlpha{marker}", "Alphaville")
    location_b = _create_location(engine, f"SearchCityBeta{marker}", "Betaville")

    group_a = _create_group(engine, f"SearchGroupAlpha{marker}", location_a.id)
    group_b = _create_group(engine, f"SearchGroupBeta{marker}", location_b.id)

    super_admin = _create_user(engine, "SUPER_ADMIN", _next_phone())
    group_admin = _create_user(engine, "GROUP_ADMIN", _next_phone(), group_a.id)

    member_a_user = _create_user(engine, "MEMBER", _next_phone())
    member_b_user = _create_user(engine, "MEMBER", _next_phone())

    member_a = _create_member(
        engine,
        member_a_user.id,
        group_a.id,
        first_name=f"ZoravarAlpha{marker}",
        membership_number=f"SEARCH-A-{marker}",
        location_id=location_a.id,
    )
    member_b = _create_member(
        engine,
        member_b_user.id,
        group_b.id,
        first_name=f"ZoravarBeta{marker}",
        membership_number=f"SEARCH-B-{marker}",
        location_id=location_b.id,
    )

    # A shared keyword across several members, for the pagination test.
    paged_users = [
        _create_user(engine, "MEMBER", _next_phone()) for _ in range(3)
    ]
    paged_members = [
        _create_member(
            engine,
            user.id,
            group_a.id,
            first_name=f"Paginate{marker}",
        )
        for user in paged_users
    ]

    created = {
        "engine": engine,
        "marker": marker,
        "group_a_id": group_a.id,
        "group_b_id": group_b.id,
        "location_a_id": location_a.id,
        "location_b_id": location_b.id,
        "member_a_id": member_a.id,
        "member_b_id": member_b.id,
        "member_a_name": member_a.first_name,
        "member_b_name": member_b.first_name,
        "member_a_number": member_a.membership_number,
        "paged_member_ids": [m.id for m in paged_members],
        "tokens": {
            "SUPER_ADMIN": _login(client, super_admin.phone_number),
            "GROUP_ADMIN": _login(client, group_admin.phone_number),
        },
        "user_ids": [
            super_admin.id,
            group_admin.id,
            member_a_user.id,
            member_b_user.id,
            *[u.id for u in paged_users],
        ],
    }

    yield created

    with Session(engine) as session:
        member_ids = [member_a.id, member_b.id, *[m.id for m in paged_members]]
        session.execute(delete(Member).where(Member.id.in_(member_ids)))
        session.execute(
            update(User).where(User.id.in_(created["user_ids"])).values(group_id=None)
        )
        session.execute(delete(User).where(User.id.in_(created["user_ids"])))
        session.execute(
            delete(SocialGroup).where(
                SocialGroup.id.in_([group_a.id, group_b.id])
            )
        )
        session.execute(
            delete(Location).where(Location.id.in_([location_a.id, location_b.id]))
        )
        session.commit()
    engine.dispose()


def test_search_requires_authentication(client, world):
    resp = client.get("/api/v1/search?q=anything")
    assert resp.status_code == 401, resp.text


def test_search_requires_a_query(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.get("/api/v1/search", headers=headers)
    assert resp.status_code == 422, resp.text


def test_search_members_by_name(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.get(
        f"/api/v1/search?q={world['member_a_name']}&type=members", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["data"]["members"]["items"]
    ids = {item["id"] for item in items}
    assert str(world["member_a_id"]) in ids
    assert str(world["member_b_id"]) not in ids
    # The member hit carries the full profile shape, not a trimmed one.
    assert "full_name" in items[0]
    assert body["data"]["members"]["pagination"]["total"] >= 1
    assert "groups" not in body["data"]


def test_search_member_by_id(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.get(
        f"/api/v1/search?q={world['member_a_id']}&type=members", headers=headers
    )
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["data"]["members"]["items"]}
    assert ids == {str(world["member_a_id"])}


def test_search_member_by_membership_number(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.get(
        f"/api/v1/search?q={world['member_a_number']}&type=members", headers=headers
    )
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["data"]["members"]["items"]}
    assert str(world["member_a_id"]) in ids


def test_search_groups_by_name(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.get(
        f"/api/v1/search?q=SearchGroupAlpha{world['marker']}&type=groups",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["groups"]["items"]
    ids = {item["id"] for item in items}
    assert str(world["group_a_id"]) in ids
    assert str(world["group_b_id"]) not in ids
    assert "members" not in resp.json()["data"]


def test_search_groups_by_member(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.get(
        f"/api/v1/search?q={world['member_a_name']}&type=groups", headers=headers
    )
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["data"]["groups"]["items"]}
    assert str(world["group_a_id"]) in ids
    assert str(world["group_b_id"]) not in ids


def test_search_groups_by_location(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.get(
        f"/api/v1/search?q=SearchCityAlpha{world['marker']}&type=groups",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["groups"]["items"]
    ids = {item["id"] for item in items}
    assert str(world["group_a_id"]) in ids
    assert str(world["group_b_id"]) not in ids


def test_search_groups_report_location_and_member_count(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.get(
        f"/api/v1/search?q=SearchGroupAlpha{world['marker']}&type=groups",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    item = next(
        i
        for i in resp.json()["data"]["groups"]["items"]
        if i["id"] == str(world["group_a_id"])
    )
    assert item["location_name"] == f"SearchCityAlpha{world['marker']}"
    assert item["member_count"] >= 1


def test_search_type_all_returns_both_sections(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.get(
        f"/api/v1/search?q={world['member_a_name']}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "members" in data and "groups" in data
    assert resp.json()["query"]["type"] == "all"


def test_search_members_is_paginated(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.get(
        f"/api/v1/search?q=Paginate{world['marker']}&type=members"
        "&page=1&page_size=2",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    section = resp.json()["data"]["members"]
    assert len(section["items"]) == 2
    assert section["pagination"]["total"] >= 3
    assert section["pagination"]["total_pages"] >= 2


def test_group_admin_search_is_scoped(client, world):
    headers = _auth(world["tokens"]["GROUP_ADMIN"])

    resp = client.get(
        f"/api/v1/search?q={world['member_b_name']}&type=members", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["members"]["items"] == []

    resp = client.get("/api/v1/search?q=SearchGroup&type=groups", headers=headers)
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["data"]["groups"]["items"]}
    assert str(world["group_a_id"]) in ids
    assert str(world["group_b_id"]) not in ids
