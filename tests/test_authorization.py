from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, text, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.constants import RoleName
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
    suffix = str(int(time.time() * 1000))[-9:]
    return f"+916{suffix}"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_user(
    engine, role_name: str, phone: str, group_id=None
) -> User:
    with Session(engine) as session:
        role = session.execute(
            select(Role).where(Role.name == role_name)
        ).scalar_one()
        user = User(
            phone_number=phone,
            password_hash=hash_password(PASSWORD),
            full_name=f"Test {role_name}",
            is_email_verified=True,
            is_phone_verified=True,
            role_id=role.id,
            group_id=group_id,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _create_group(engine, name: str) -> SocialGroup:
    with Session(engine) as session:
        group = SocialGroup(name=name)
        session.add(group)
        session.commit()
        session.refresh(group)
        return group


def _create_member(engine, user_id, group_id) -> Member:
    with Session(engine) as session:
        member = Member(
            user_id=user_id,
            group_id=group_id,
            first_name="Test",
            last_name="Member",
        )
        session.add(member)
        session.commit()
        session.refresh(member)
        return member


def _register_member(
    client: TestClient, phone: str
) -> str:
    email = f"autz_{int(time.time() * 1000)}@test.local"
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Plain Member",
            "email": email,
            "password": PASSWORD,
            "phone_number": phone.removeprefix("+91"),
            "confirm_password": PASSWORD,
            "id_token": phone,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["tokens"]["access_token"]


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

    from app.core.exceptions import UnauthorizedException

    def _fake_verify_id_token(id_token: str) -> dict:
        if id_token == "invalid-token":
            raise UnauthorizedException("Invalid or expired Firebase ID token")
        return {"uid": f"uid-{id_token}", "phone_number": id_token}

    with mock.patch(
        "app.services.auth_service.firebase_verify_id_token", _fake_verify_id_token
    ):
        yield


@pytest.fixture(scope="module")
def world(client):
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    group_a = _create_group(engine, f"Autz Group A {uuid.uuid4()}")
    group_b = _create_group(engine, f"Autz Group B {uuid.uuid4()}")

    # Three roles only: SUPER_ADMIN, GROUP_ADMIN, MEMBER.
    admin = _create_user(engine, "SUPER_ADMIN", _next_phone())
    super_admin = _create_user(engine, "SUPER_ADMIN", _next_phone())
    # Two Group Admins in different groups, to prove each is limited to its own.
    group_admin = _create_user(engine, "GROUP_ADMIN", _next_phone(), group_a.id)
    foreign_group_admin = _create_user(
        engine, "GROUP_ADMIN", _next_phone(), group_b.id
    )
    target_a = _create_user(engine, "MEMBER", _next_phone())
    target_b = _create_user(engine, "MEMBER", _next_phone())
    free_user = _create_user(engine, "MEMBER", _next_phone())
    role_target = _create_user(engine, "MEMBER", _next_phone())

    _create_member(engine, group_admin.id, group_a.id)
    _create_member(engine, foreign_group_admin.id, group_b.id)
    member_a = _create_member(engine, target_a.id, group_a.id)
    member_b = _create_member(engine, target_b.id, group_b.id)

    member_phone = _next_phone()
    member_token = _register_member(client, member_phone)
    with Session(engine) as session:
        member_user = session.execute(
            select(User).where(User.phone_number == member_phone)
        ).scalar_one()

    tokens = {
        "member": member_token,
        "SUPER_ADMIN": _login(client, admin.phone_number),
        "SUPER_ADMIN_2": _login(client, super_admin.phone_number),
        "GROUP_ADMIN": _login(client, group_admin.phone_number),
        "FOREIGN_GROUP_ADMIN": _login(client, foreign_group_admin.phone_number),
    }

    created = {
        "engine": engine,
        "group_a_id": group_a.id,
        "group_b_id": group_b.id,
        "group_admin_id": group_admin.id,
        "foreign_group_admin_id": foreign_group_admin.id,
        "member_user_id": member_user.id,
        "member_a_id": member_a.id,
        "member_b_id": member_b.id,
        "target_a_id": target_a.id,
        "target_b_id": target_b.id,
        "free_user_id": free_user.id,
        "role_target_id": role_target.id,
        "tokens": tokens,
        "user_ids": [
            admin.id,
            super_admin.id,
            group_admin.id,
            foreign_group_admin.id,
            target_a.id,
            target_b.id,
            free_user.id,
            role_target.id,
            member_user.id,
        ],
    }

    yield created

    with Session(engine) as session:
        session.execute(
            delete(Member).where(Member.user_id.in_(created["user_ids"]))
        )
        session.execute(
            update(User).where(User.id.in_(created["user_ids"])).values(group_id=None)
        )
        session.execute(delete(User).where(User.id.in_(created["user_ids"])))
        session.execute(
            delete(SocialGroup).where(
                SocialGroup.id.in_([created["group_a_id"], created["group_b_id"]])
            )
        )
        session.commit()
    engine.dispose()


def test_admin_endpoints_require_authentication(client, world):
    resp = client.post("/api/v1/groups", json={"name": f"Needs Auth {uuid.uuid4()}"})
    assert resp.status_code == 401, resp.text

    resp = client.patch(
        f"/api/v1/users/{uuid.uuid4()}", json={"full_name": "Hacker"}
    )
    assert resp.status_code == 401, resp.text


def test_member_forbidden_on_admin_endpoints(client, world):
    headers = _auth(world["tokens"]["member"])

    resp = client.post(
        "/api/v1/groups", json={"name": f"Member Group {uuid.uuid4()}"}, headers=headers
    )
    assert resp.status_code == 403, resp.text

    resp = client.get("/api/v1/users", headers=headers)
    assert resp.status_code == 403, resp.text

    resp = client.patch(
        f"/api/v1/users/{world['member_user_id']}/role",
        json={"role_name": "SUPER_ADMIN"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text

    resp = client.post(
        f"/api/v1/members/{world['member_a_id']}/approve",
        json={"status": "APPROVED"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text


def test_super_admin_is_platform_wide(client, world):
    for token in ("SUPER_ADMIN", "SUPER_ADMIN_2"):
        headers = _auth(world["tokens"][token])
        name = f"{token} Group {uuid.uuid4()}"
        resp = client.post("/api/v1/groups", json={"name": name}, headers=headers)
        assert resp.status_code in (200, 201), resp.text

        resp = client.get("/api/v1/users", headers=headers)
        assert resp.status_code == 200, resp.text


def test_group_admin_cannot_reach_platform_wide_endpoints(client, world):
    """A GROUP_ADMIN is scoped, so it must not inherit SUPER_ADMIN's reach."""
    headers = _auth(world["tokens"]["GROUP_ADMIN"])

    assert client.get("/api/v1/users", headers=headers).status_code == 403
    assert (
        client.post(
            "/api/v1/groups", json={"name": f"GA Group {uuid.uuid4()}"}, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/users/{world['role_target_id']}/role",
            json={"role_name": "SUPER_ADMIN"},
            headers=headers,
        ).status_code
        == 403
    )


def test_super_admin_can_manage_user_roles(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.patch(
        f"/api/v1/users/{world['role_target_id']}/role",
        json={
            "role_name": "GROUP_ADMIN",
            "group_id": str(world["group_a_id"]),
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["role"]["name"] == "GROUP_ADMIN"
    assert body["group_id"] == str(world["group_a_id"])

    # Demoting clears the group scope, so the account cannot keep group access.
    resp = client.patch(
        f"/api/v1/users/{world['role_target_id']}/role",
        json={"role_name": "MEMBER"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["group_id"] is None


def test_group_admin_role_requires_a_group(client, world):
    """Without a group a Group Admin would be either locked out or unscoped."""
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.patch(
        f"/api/v1/users/{world['role_target_id']}/role",
        json={"role_name": "GROUP_ADMIN"},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text

    resp = client.patch(
        f"/api/v1/users/{world['role_target_id']}/role",
        json={"role_name": "GROUP_ADMIN", "group_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert resp.status_code == 404, resp.text


def test_role_change_via_users_patch_is_rejected(client, world):
    """Role changes belong to /role, which also owns the group bookkeeping."""
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.patch(
        f"/api/v1/users/{world['role_target_id']}",
        json={"role_name": "GROUP_ADMIN"},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


def test_super_admin_cannot_demote_itself(client, world):
    engine = world["engine"]
    phone = _next_phone()
    admin = _create_user(engine, "SUPER_ADMIN", phone)
    headers = _auth(_login(client, phone))

    resp = client.patch(
        f"/api/v1/users/{admin.id}/role",
        json={"role_name": "MEMBER"},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text

    with Session(engine) as session:
        session.execute(delete(User).where(User.id == admin.id))
        session.commit()


def test_member_cannot_change_roles(client, world):
    headers = _auth(world["tokens"]["member"])
    resp = client.patch(
        f"/api/v1/users/{world['role_target_id']}/role",
        json={"role_name": "SUPER_ADMIN"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text


def test_role_change_requires_authentication(client, world):
    resp = client.patch(
        f"/api/v1/users/{world['role_target_id']}/role",
        json={"role_name": "SUPER_ADMIN"},
    )
    assert resp.status_code == 401, resp.text


def test_oauth2_token_endpoint_super_admin_only(client, world):
    engine = world["engine"]

    admin_phone = _next_phone()
    _create_user(engine, "SUPER_ADMIN", admin_phone)

    member_phone = _next_phone()
    _register_member(client, member_phone)

    token_resp = client.post(
        "/api/v1/auth/token",
        data={"username": admin_phone, "password": PASSWORD},
    )
    assert token_resp.status_code == 200, token_resp.text
    body = token_resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]

    authed = client.get("/api/v1/users", headers=_auth(body["access_token"]))
    assert authed.status_code == 200, authed.text

    wrong_pw = client.post(
        "/api/v1/auth/token",
        data={"username": admin_phone, "password": "WrongPass123!"},
    )
    assert wrong_pw.status_code == 401, wrong_pw.text

    member_resp = client.post(
        "/api/v1/auth/token",
        data={"username": member_phone, "password": PASSWORD},
    )
    assert member_resp.status_code == 403, member_resp.text

    login_resp = client.post(
        "/api/v1/auth/login",
        json={"phone_number": admin_phone, "password": PASSWORD},
    )
    assert login_resp.status_code == 200, login_resp.text
    assert login_resp.json()["tokens"]["access_token"]

    _delete_users_by_phone(engine, [admin_phone, member_phone])


def _delete_users_by_phone(engine, phones: list[str]) -> None:
    with Session(engine) as session:
        session.execute(delete(User).where(User.phone_number.in_(phones)))
        session.commit()


def test_signup_rejects_role_field(client):
    engine = create_engine(_psycopg_url(), poolclass=NullPool)

    member_phone = _next_phone()
    body = {
        "full_name": "No Role",
        "email": f"norole_{int(time.time() * 1000)}@test.local",
        "password": PASSWORD,
        "phone_number": member_phone.removeprefix("+91"),
        "confirm_password": PASSWORD,
        "id_token": member_phone,
    }
    resp = client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["role"]["name"] == "MEMBER"

    role_phone = _next_phone()
    role_body = {
        **body,
        "full_name": "Role Field",
        "email": f"rolefield_{int(time.time() * 1000)}@test.local",
        "phone_number": role_phone.removeprefix("+91"),
        "id_token": role_phone,
        "role": "Admin",
    }
    resp = client.post("/api/v1/auth/register", json=role_body)
    assert resp.status_code == 422

    _delete_users_by_phone(engine, [member_phone])
    engine.dispose()


def test_login_response_includes_role(client):
    engine = create_engine(_psycopg_url(), poolclass=NullPool)

    member_phone = _next_phone()
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Role Member",
            "email": f"rolemember_{int(time.time() * 1000)}@test.local",
            "password": PASSWORD,
            "phone_number": member_phone.removeprefix("+91"),
            "confirm_password": PASSWORD,
            "id_token": member_phone,
        },
    )
    assert resp.status_code == 200, resp.text
    login = client.post(
        "/api/v1/auth/login",
        json={"phone_number": member_phone, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["role"]["name"] == "MEMBER"

    admin_phone = _next_phone()
    _create_user(engine, "SUPER_ADMIN", admin_phone)
    login = client.post(
        "/api/v1/auth/login",
        json={"phone_number": admin_phone, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["role"]["name"] == "SUPER_ADMIN"

    _delete_users_by_phone(engine, [member_phone, admin_phone])
    engine.dispose()


def test_logout_still_works(client):
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    phone = _next_phone()
    email = f"logout_{int(time.time() * 1000)}@test.local"
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Logout Probe",
            "email": email,
            "password": PASSWORD,
            "phone_number": phone.removeprefix("+91"),
            "confirm_password": PASSWORD,
            "id_token": phone,
        },
    )
    assert resp.status_code == 200, resp.text
    access = resp.json()["tokens"]["access_token"]
    refresh = resp.json()["tokens"]["refresh_token"]

    resp = client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh},
        headers=_auth(access),
    )
    assert resp.status_code == 200, resp.text

    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 401, resp.text

    _delete_users_by_phone(engine, [phone])
    engine.dispose()


def test_group_admin_scoped_to_own_group(client, world):
    headers = _auth(world["tokens"]["GROUP_ADMIN"])

    resp = client.patch(
        f"/api/v1/members/{world['member_a_id']}",
        json={"first_name": "Alice"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    resp = client.patch(
        f"/api/v1/members/{world['member_b_id']}",
        json={"first_name": "Bob"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text

    starts_at = datetime.now(UTC).isoformat()
    resp = client.post(
        "/api/v1/events",
        json={
            "title": f"Own group event {uuid.uuid4()}",
            "starts_at": starts_at,
            "group_id": str(world["group_a_id"]),
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text

    resp = client.post(
        "/api/v1/events",
        json={
            "title": f"Foreign event {uuid.uuid4()}",
            "starts_at": starts_at,
            "group_id": str(world["group_b_id"]),
        },
        headers=headers,
    )
    assert resp.status_code == 403, resp.text

    resp = client.post(
        "/api/v1/members",
        json={
            "user_id": str(world["free_user_id"]),
            "first_name": "Free",
            "last_name": "User",
            "full_name": "Free User",
            "phone_number": "9000000009",
            "group_id": str(world["group_b_id"]),
        },
        headers=headers,
    )
    assert resp.status_code == 403, resp.text


def test_group_admin_in_another_group_sees_nothing(client, world):
    """Two Group Admios, different groups: each is confined to its own."""
    headers = _auth(world["tokens"]["FOREIGN_GROUP_ADMIN"])

    # Own group (B): allowed.
    resp = client.patch(
        f"/api/v1/members/{world['member_b_id']}",
        json={"first_name": "Frank"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # Group A: refused.
    resp = client.patch(
        f"/api/v1/members/{world['member_a_id']}",
        json={"first_name": "Grace"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text

    resp = client.post(
        "/api/v1/events",
        json={
            "title": f"Foreign group event {uuid.uuid4()}",
            "starts_at": datetime.now(UTC).isoformat(),
            "group_id": str(world["group_a_id"]),
        },
        headers=headers,
    )
    assert resp.status_code == 403, resp.text

    # Cross-group reads are 404, not 403: the id must not be confirmed to exist.
    assert (
        client.get(
            f"/api/v1/members/{world['member_a_id']}", headers=headers
        ).status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/groups/{world['group_a_id']}", headers=headers).status_code
        == 404
    )


# --- Group isolation of every read endpoint ----------------------------------------
# The write paths (PATCH/POST/DELETE) were group-scoped, but the reads were not:
# GET /members, /trustees, /events, /news, /media and /groups accepted no
# authentication at all, so a GROUP_ADMIN could enumerate the whole platform.


def test_member_listing_requires_authentication(client, world):
    """A read that accepts no credentials is a read that leaks every group."""
    for path in (
        "/api/v1/members",
        "/api/v1/groups",
        "/api/v1/trustees",
        "/api/v1/events",
        "/api/v1/news",
    ):
        resp = client.get(path)
        assert resp.status_code == 401, f"{path} -> {resp.text}"

    resp = client.get(
        f"/api/v1/members/{world['member_a_id']}",
    )
    assert resp.status_code == 401, resp.text


def test_group_admin_member_listing_excludes_other_groups(client, world):
    headers = _auth(world["tokens"]["GROUP_ADMIN"])
    resp = client.get("/api/v1/members?page_size=100", headers=headers)
    assert resp.status_code == 200, resp.text
    groups = {
        item["group_id"] for item in resp.json()["data"] if item.get("group_id")
    }
    assert str(world["group_a_id"]) in groups
    assert str(world["group_b_id"]) not in groups

    # Asking for another group is refused rather than silently redirected.
    resp = client.get(
        f"/api/v1/members?group_id={world['group_b_id']}", headers=headers
    )
    assert resp.status_code == 403, resp.text


def test_member_search_still_honours_group_scope(client, world):
    """Search used to ignore group_id entirely, which leaked across groups."""
    engine = world["engine"]
    marker = f"Scoped{uuid.uuid4().hex[:8]}"
    a_target = _create_user(engine, "MEMBER", _next_phone())
    b_target = _create_user(engine, "MEMBER", _next_phone())
    a_member = _create_member(engine, a_target.id, world["group_a_id"])
    b_member = _create_member(engine, b_target.id, world["group_b_id"])
    for first, member in ((marker, a_member), (marker, b_member)):
        with Session(engine) as session:
            row = session.get(Member, member.id)
            row.first_name = first
            session.commit()

    try:
        headers = _auth(world["tokens"]["GROUP_ADMIN"])
        resp = client.get(
            f"/api/v1/members?query={marker}&page_size=100", headers=headers
        )
        assert resp.status_code == 200, resp.text
        found = {item["id"] for item in resp.json()["data"]}
        assert str(a_member.id) in found
        assert str(b_member.id) not in found
    finally:
        with Session(engine) as session:
            session.execute(
                delete(Member).where(Member.id.in_([a_member.id, b_member.id]))
            )
            session.execute(delete(User).where(User.id.in_([a_target.id, b_target.id])))
            session.commit()


def test_group_admin_sees_only_its_own_group(client, world):
    headers = _auth(world["tokens"]["GROUP_ADMIN"])
    resp = client.get("/api/v1/groups?page_size=100", headers=headers)
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["data"]}
    assert ids == {str(world["group_a_id"])}

    # Search is scoped too, otherwise the picker leaks the rest of the platform.
    resp = client.get(
        f"/api/v1/groups?query=Group B {world['group_b_id']}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


def test_group_admin_listings_are_group_scoped(client, world):
    headers = _auth(world["tokens"]["GROUP_ADMIN"])
    for path in ("/api/v1/trustees", "/api/v1/events", "/api/v1/news"):
        resp = client.get(f"{path}?page_size=100", headers=headers)
        assert resp.status_code == 200, f"{path} -> {resp.text}"
        for item in resp.json()["data"]:
            assert item.get("group_id") == str(world["group_a_id"]), item


def test_super_admin_sees_platform_wide_data(client, world):
    """A SUPER_ADMIN is unrestricted, so NULL group_id rows stay visible to it."""
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    for path in ("/api/v1/trustees", "/api/v1/events", "/api/v1/news"):
        resp = client.get(f"{path}?page_size=100", headers=headers)
        assert resp.status_code == 200, f"{path} -> {resp.text}"


def test_group_admin_cannot_create_platform_wide_news(client, world):
    """A GROUP_ADMIN with no group must not create an unscoped, invisible item."""
    engine = world["engine"]
    phone = _next_phone()
    unassigned = _create_user(engine, "GROUP_ADMIN", phone)
    try:
        headers = _auth(_login(client, phone))
        resp = client.post(
            "/api/v1/news",
            json={"title": f"Orphan news {uuid.uuid4()}"},
            headers=headers,
        )
        assert resp.status_code == 403, resp.text
    finally:
        with Session(engine) as session:
            session.execute(delete(User).where(User.id == unassigned.id))
            session.commit()


def test_group_admin_media_is_group_scoped(client, world):
    headers = _auth(world["tokens"]["GROUP_ADMIN"])
    # Group listing is allowed for its own group, refused for another.
    assert (
        client.get(
            f"/api/v1/media?group_id={world['group_a_id']}", headers=headers
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/v1/media?group_id={world['group_b_id']}", headers=headers
        ).status_code
        == 403
    )
    # Browsing another group's member media is refused too.
    assert (
        client.get(
            "/api/v1/media"
            f"?owner_type=member&owner_id={world['member_b_id']}",
            headers=headers,
        ).status_code
        == 403
    )
    # Own group's member media is allowed.
    assert (
        client.get(
            "/api/v1/media"
            f"?owner_type=member&owner_id={world['member_a_id']}",
            headers=headers,
        ).status_code
        == 200
    )


def test_media_listing_requires_a_selector(client, world):
    """An owner/group selector is mandatory: a bare list would expose everything."""
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    assert client.get("/api/v1/media", headers=headers).status_code == 400
    assert (
        client.get(
            "/api/v1/media?owner_type=member", headers=headers
        ).status_code
        == 400
    )
    assert (
        client.get(
            f"/api/v1/media?owner_type=member&owner_id={world['member_a_id']}"
            f"&group_id={world['group_a_id']}",
            headers=headers,
        ).status_code
        == 400
    )


# --- Group administration ---------------------------------------------------------
# There was no way to assign a Group Admin to a group, so scope resolution could
# only ever guess from the member record.


def test_super_admin_assigns_and_removes_group_admin(client, world):
    engine = world["engine"]
    target = _create_user(engine, "MEMBER", _next_phone())
    try:
        headers = _auth(world["tokens"]["SUPER_ADMIN"])
        resp = client.post(
            f"/api/v1/groups/{world['group_a_id']}/admins",
            json={"user_id": str(target.id)},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["admin"]["id"] == str(target.id)

        with Session(engine) as session:
            row = session.get(User, target.id)
            assert row.role.name == "GROUP_ADMIN"
            assert row.group_id == world["group_a_id"]

        # The assignment is reflected on the group payload.
        resp = client.get(
            f"/api/v1/groups/{world['group_a_id']}", headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert str(target.id) in {
            a["id"] for a in resp.json()["data"]["admins"]
        }

        # Reassigning the same admin to the same group is idempotent.
        resp = client.post(
            f"/api/v1/groups/{world['group_a_id']}/admins",
            json={"user_id": str(target.id)},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        resp = client.delete(
            f"/api/v1/groups/{world['group_a_id']}/admins/{target.id}",
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        with Session(engine) as session:
            row = session.get(User, target.id)
            assert row.role.name == "MEMBER"
            assert row.group_id is None
    finally:
        with Session(engine) as session:
            session.execute(
                update(User).where(User.id == target.id).values(group_id=None)
            )
            session.execute(delete(User).where(User.id == target.id))
            session.commit()


def test_group_admin_cannot_assign_admins(client, world):
    """Otherwise a Group Admin could promote a peer and widen its own reach."""
    headers = _auth(world["tokens"]["GROUP_ADMIN"])
    resp = client.post(
        f"/api/v1/groups/{world['group_a_id']}/admins",
        json={"user_id": str(world["free_user_id"])},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text


def test_group_admin_cannot_assign_to_another_group(client, world):
    headers = _auth(world["tokens"]["GROUP_ADMIN"])
    resp = client.post(
        f"/api/v1/groups/{world['group_b_id']}/admins",
        json={"user_id": str(world["free_user_id"])},
        headers=headers,
    )
    # The role permission is SUPER_ADMIN-only, so this never reaches group logic.
    assert resp.status_code == 403, resp.text


def test_super_admin_adds_and_removes_group_member(client, world):
    engine = world["engine"]
    target = _create_user(engine, "MEMBER", _next_phone())
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    try:
        # A login with no member profile yet gets one, so a new member can be
        # added to a group without a second step.
        resp = client.post(
            f"/api/v1/groups/{world['group_b_id']}/members",
            json={"user_id": str(target.id)},
            headers=headers,
        )
        assert resp.status_code in (200, 201), resp.text
        member_id = resp.json()["data"]["id"]

        resp = client.get(
            f"/api/v1/groups/{world['group_b_id']}/members?page_size=100",
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert member_id in {m["id"] for m in resp.json()["data"]}

        # Removal keeps the member profile and only clears the group link.
        resp = client.delete(
            f"/api/v1/groups/{world['group_b_id']}/members/{member_id}",
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        with Session(engine) as session:
            assert session.get(Member, member_id).group_id is None

        resp = client.delete(
            f"/api/v1/groups/{world['group_b_id']}/members/{member_id}",
            headers=headers,
        )
        assert resp.status_code == 404, resp.text
    finally:
        with Session(engine) as session:
            session.execute(delete(Member).where(Member.user_id == target.id))
            session.execute(delete(User).where(User.id == target.id))
            session.commit()


def test_group_admin_adds_member_to_own_group_only(client, world):
    engine = world["engine"]
    headers = _auth(world["tokens"]["GROUP_ADMIN"])
    target = _create_user(engine, "MEMBER", _next_phone())
    try:
        resp = client.post(
            f"/api/v1/groups/{world['group_a_id']}/members",
            json={"user_id": str(target.id)},
            headers=headers,
        )
        assert resp.status_code in (200, 201), resp.text

        resp = client.post(
            f"/api/v1/groups/{world['group_b_id']}/members",
            json={"user_id": str(world["free_user_id"])},
            headers=headers,
        )
        assert resp.status_code == 403, resp.text
    finally:
        with Session(engine) as session:
            session.execute(delete(Member).where(Member.user_id == target.id))
            session.execute(delete(User).where(User.id == target.id))
            session.commit()


def test_group_admin_cannot_steal_a_member_from_another_group(client, world):
    """Adding a member from another group would be a cross-group write."""
    headers = _auth(world["tokens"]["GROUP_ADMIN"])
    resp = client.post(
        f"/api/v1/groups/{world['group_a_id']}/members",
        json={"member_id": str(world["member_b_id"])},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text

    engine = world["engine"]
    with Session(engine) as session:
        assert session.get(Member, world["member_b_id"]).group_id == world[
            "group_b_id"
        ]


def test_super_admin_may_move_a_member_between_groups(client, world):
    engine = world["engine"]
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.post(
        f"/api/v1/groups/{world['group_a_id']}/members",
        json={"member_id": str(world["member_b_id"])},
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text

    try:
        with Session(engine) as session:
            assert session.get(Member, world["member_b_id"]).group_id == world[
                "group_a_id"
            ]
    finally:
        with Session(engine) as session:
            session.execute(
                text("UPDATE members SET group_id = :g WHERE id = :i"),
                {"g": world["group_b_id"], "i": world["member_b_id"]},
            )
            session.commit()


def test_trustee_can_be_promoted_from_a_member(client, world):
    engine = world["engine"]
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.post(
        "/api/v1/trustees",
        json={
            "group_id": str(world["group_a_id"]),
            "member_id": str(world["member_a_id"]),
            "first_name": "",
            "last_name": "",
            "designation": "Treasurer",
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text
    trustee = resp.json()["data"]
    assert trustee["member_id"] == str(world["member_a_id"])
    # The member's name is filled in from the member record.
    assert trustee["first_name"]
    assert trustee["last_name"]

    with Session(engine) as session:
        session.execute(
            text("DELETE FROM trustees WHERE id = :id"), {"id": trustee["id"]}
        )
        session.commit()


def test_trustee_must_belong_to_the_board_group(client, world):
    """A trustee linked to a member of another group would disagree about its group."""
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    resp = client.post(
        "/api/v1/trustees",
        json={
            "group_id": str(world["group_b_id"]),
            "member_id": str(world["member_a_id"]),
            "first_name": "Wrong",
            "last_name": "Group",
            "designation": "Treasurer",
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


def test_full_member_profile_is_editable_by_admin(client, world):
    """PATCH /members must accept the profile fields, not just the member columns."""
    engine = world["engine"]
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    member = _create_userless_member(
        client, headers, f"Profile {uuid.uuid4()}", "9876500888"
    )

    resp = client.patch(
        f"/api/v1/members/{member['id']}",
        json={
            "first_name": "Edited",
            "last_name": "Profile",
            "spouse_name": "Sunita",
            "member_occupation": "Business",
            "company_name": "Acme",
            "group_designation": "President",
            "interest_fields": "Chess",
            "address": "Sector A",
            "city": "Pune",
            "area": "Kothrud",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["first_name"] == "Edited"
    assert body["spouse_name"] == "Sunita"
    assert body["member_occupation"] == "Business"
    assert body["company_name"] == "Acme"
    assert body["group_designation"] == "President"
    assert body["interest_fields"] == "Chess"
    assert body["address"] == "Sector A"
    assert body["city"] == "Pune"

    with Session(engine) as session:
        session.execute(delete(Member).where(Member.id == member["id"]))
        session.commit()


def test_login_response_carries_group_context(client, world):
    """A client must be able to tell which group an admin is limited to."""
    engine = world["engine"]
    target = _create_user(
        engine, "GROUP_ADMIN", _next_phone(), world["group_a_id"]
    )
    try:
        login = client.post(
            "/api/v1/auth/login",
            json={"phone_number": target.phone_number, "password": PASSWORD},
        )
        assert login.status_code == 200, login.text
        user = login.json()["user"]
        assert user["role"]["name"] == "GROUP_ADMIN"
        assert user["group_id"] == str(world["group_a_id"])
        assert user["group_name"]
    finally:
        with Session(engine) as session:
            session.execute(
                update(User).where(User.id == target.id).values(group_id=None)
            )
            session.execute(delete(User).where(User.id == target.id))
            session.commit()


# --- Regression: role names polluted by the Excel / Google Sheet import -------------
# Production stored the ADMIN role as "Admin\n". Authorization looks roles up by
# exact name, so those users matched no ROLE_PERMISSIONS key and every member write
# returned 403 "Missing permission: member.update". See migration c7d2e1f0a3b4.


def _create_polluted_role_user(engine, stored_role_name: str, phone: str) -> User:
    """Create a user whose roles row carries a whitespace/case polluted name."""
    with Session(engine) as session:
        role = Role(name=stored_role_name, description="Registered community member")
        session.add(role)
        session.flush()
        user = User(
            phone_number=phone,
            password_hash=hash_password(PASSWORD),
            full_name="Polluted Role Admin",
            is_email_verified=True,
            is_phone_verified=True,
            role_id=role.id,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@pytest.mark.parametrize("stored", ["Admin\n", "Admin", "admin", " admin "])
def test_polluted_admin_role_name_still_authorizes_member_writes(client, world, stored):
    engine = world["engine"]
    phone = _next_phone()
    admin = _create_polluted_role_user(engine, stored, phone)
    group = _create_group(engine, f"Polluted Group {uuid.uuid4()}")
    target = _create_user(engine, "MEMBER", _next_phone())
    member = _create_member(engine, target.id, None)  # group-less, as in production
    headers = _auth(_login(client, phone))

    try:
        resp = client.post(
            "/api/v1/members",
            json={
                "user_id": str(world["free_user_id"]),
                "full_name": "Created By Polluted Admin",
                "phone_number": "+919876500000",
            },
            headers=headers,
        )
        assert resp.status_code in (200, 201), resp.text

        resp = client.patch(
            f"/api/v1/members/{member.id}",
            json={"occupation_summary": "edited by polluted admin"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["occupation_summary"] == "edited by polluted admin"

        resp = client.delete(f"/api/v1/members/{member.id}", headers=headers)
        assert resp.status_code == 200, resp.text
    finally:
        with Session(engine) as session:
            session.execute(delete(Member).where(Member.user_id == admin.id))
            session.execute(delete(Member).where(Member.id == member.id))
            session.execute(
                delete(Member).where(Member.user_id == world["free_user_id"])
            )
            session.execute(delete(User).where(User.id.in_([admin.id, target.id])))
            session.execute(
                delete(Role).where(Role.name == stored)
            )
            session.execute(
                delete(SocialGroup).where(SocialGroup.id == group.id)
            )
            session.commit()


def test_polluted_member_role_name_gets_no_write_permissions(client, world):
    """The fix must not escalate anyone: a polluted MEMBER row still cannot write."""
    engine = world["engine"]
    phone = _next_phone()
    fake_member = _create_polluted_role_user(engine, "Member\n", phone)
    target = _create_user(engine, "MEMBER", _next_phone())
    member = _create_member(engine, target.id, None)
    headers = _auth(_login(client, phone))

    try:
        for method, path, kwargs in (
            ("post", "/api/v1/members", {"json": {"user_id": str(world["free_user_id"]),
                                                   "full_name": "Nope",
                                                   "phone_number": "+919876500001"}}),
            (
                "patch",
                f"/api/v1/members/{member.id}",
                {"json": {"occupation_summary": "x"}},
            ),
            ("delete", f"/api/v1/members/{member.id}", {}),
        ):
            resp = getattr(client, method)(path, headers=headers, **kwargs)
            assert resp.status_code == 403, f"{method} {path} -> {resp.text}"
    finally:
        with Session(engine) as session:
            session.execute(delete(Member).where(Member.user_id == fake_member.id))
            session.execute(delete(Member).where(Member.id == member.id))
            session.execute(
                delete(User).where(User.id.in_([fake_member.id, target.id]))
            )
            session.execute(delete(Role).where(Role.name == "Member\n"))
            session.commit()


def test_role_name_resolver_maps_canonical_and_retired_labels():
    from app.core.constants import is_canonical_role_label, resolve_role_name

    # The three surviving roles, including polluted whitespace.
    assert resolve_role_name("SUPER_ADMIN") is RoleName.SUPER_ADMIN
    assert resolve_role_name("Group_Admin\n") is RoleName.GROUP_ADMIN
    assert resolve_role_name(" group_admin ") is RoleName.GROUP_ADMIN
    assert resolve_role_name("Member\n") is RoleName.MEMBER

    # Retired tiers collapse onto the role that replaced them, so a stale role row
    # or an old token cannot lock an administrator out.
    assert resolve_role_name("ADMIN") is RoleName.SUPER_ADMIN
    assert resolve_role_name("Admin\n") is RoleName.SUPER_ADMIN
    assert resolve_role_name("federation_admin") is RoleName.SUPER_ADMIN
    assert resolve_role_name("REGIONAL_ADMIN") is RoleName.GROUP_ADMIN

    assert resolve_role_name("BOGUS") is None
    assert resolve_role_name("") is None
    assert resolve_role_name(None) is None

    # Only the three surviving labels count as canonical, which is what keeps a
    # retired tier from being re-seeded by the bootstrap service.
    assert is_canonical_role_label("SUPER_ADMIN")
    assert is_canonical_role_label(" group_admin\n")
    assert not is_canonical_role_label("ADMIN")
    assert not is_canonical_role_label(None)


def test_only_three_roles_are_seeded():
    """The bootstrap service must not resurrect a retired admin tier."""
    from app.core.constants import LEGACY_ROLE_ALIASES
    from app.core.permissions import ROLE_PERMISSIONS

    assert set(ROLE_PERMISSIONS) == {
        RoleName.SUPER_ADMIN,
        RoleName.GROUP_ADMIN,
        RoleName.MEMBER,
    }
    assert set(LEGACY_ROLE_ALIASES) == {"ADMIN", "FEDERATION_ADMIN", "REGIONAL_ADMIN"}
    assert set(LEGACY_ROLE_ALIASES.values()) <= set(ROLE_PERMISSIONS)


# --- Regression: a Member is a community profile, a User is a login account -------
# members.user_id was NOT NULL ON DELETE CASCADE, which forced every community
# member to have a login and made a login deletion able to destroy community data.
# See migrations d4e5f6a7b8c9 (user_id optional) and the panel guard that produced
# "The API requires an existing user for every member record."


def _create_userless_member(client: TestClient, headers, name: str, phone: str) -> dict:
    """Admin creates a member with no User account (e.g. a Google Sheet import)."""
    resp = client.post(
        "/api/v1/members",
        json={
            "full_name": name,
            "phone_number": phone,
            "member_dob": "1985-04-02",
            "spouse_name": "Sunita",
            "address": "Sector A",
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["data"]


def test_admin_creates_member_without_user_account(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    name = f"No Account {uuid.uuid4()}"
    member = _create_userless_member(client, headers, name, "9876500999")

    assert member["user_id"] is None
    assert member["user"] is None
    assert member["full_name"] == name
    assert member["member_dob"] == "1985-04-02"
    assert member["spouse_name"] == "Sunita"

    with Session(world["engine"]) as session:
        session.execute(
            delete(Member).where(Member.id == member["id"])
        )
        session.commit()


def test_admin_updates_and_deletes_member_without_user_account(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    member = _create_userless_member(
        client, headers, f"No Account {uuid.uuid4()}", "9876500998"
    )
    member_id = member["id"]

    resp = client.patch(
        f"/api/v1/members/{member_id}",
        json={
                "first_name": "Edited",
                "last_name": "Without",
                "occupation_summary": "Business",
            },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["user_id"] is None
    assert resp.json()["data"]["occupation_summary"] == "Business"

    resp = client.get(f"/api/v1/members/{member_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["user_id"] is None

    resp = client.delete(f"/api/v1/members/{member_id}", headers=headers)
    assert resp.status_code == 200, resp.text


def test_admin_still_updates_member_that_has_a_linked_user(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    target = _create_user(world["engine"], "MEMBER", _next_phone())
    member = _create_member(world["engine"], target.id, None)

    resp = client.patch(
        f"/api/v1/members/{member.id}",
        json={"occupation_summary": "linked edit"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["user_id"] == str(target.id)
    assert resp.json()["data"]["occupation_summary"] == "linked edit"

    with Session(world["engine"]) as session:
        session.execute(delete(Member).where(Member.id == member.id))
        session.execute(delete(User).where(User.id == target.id))
        session.commit()


def test_non_admin_cannot_touch_userless_members(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    member = _create_userless_member(
        client, headers, f"No Account {uuid.uuid4()}", "9876500997"
    )
    member_headers = _auth(world["tokens"]["member"])

    resp = client.post(
        "/api/v1/members",
        json={"full_name": "Nope", "phone_number": "9876500996"},
        headers=member_headers,
    )
    assert resp.status_code == 403, resp.text

    resp = client.patch(
        f"/api/v1/members/{member['id']}",
        json={"occupation_summary": "x"},
        headers=member_headers,
    )
    assert resp.status_code == 403, resp.text

    resp = client.delete(f"/api/v1/members/{member['id']}", headers=member_headers)
    assert resp.status_code == 403, resp.text

    with Session(world["engine"]) as session:
        session.execute(delete(Member).where(Member.id == member["id"]))
        session.commit()


def test_member_survives_a_hard_delete_of_its_user(client, world):
    """ON DELETE SET NULL: removing a login must never destroy community data."""
    engine = world["engine"]
    target = _create_user(engine, "MEMBER", _next_phone())
    member = _create_member(engine, target.id, None)

    with Session(engine) as session:
        session.execute(
            text("DELETE FROM users WHERE id = :id"), {"id": target.id}
        )
        session.commit()

    with Session(engine) as session:
        # ON DELETE SET NULL: the member row survives and simply loses its link.
        assert session.execute(
            select(Member.id).where(Member.id == member.id)
        ).scalar_one_or_none() == member.id
        assert session.execute(
            select(Member.user_id).where(Member.id == member.id)
        ).scalar_one_or_none() is None

        session.execute(delete(Member).where(Member.id == member.id))
        session.commit()


def test_two_members_may_have_no_user_account(client, world):
    headers = _auth(world["tokens"]["SUPER_ADMIN"])
    a = _create_userless_member(client, headers, f"Null A {uuid.uuid4()}", "9876500995")
    b = _create_userless_member(client, headers, f"Null B {uuid.uuid4()}", "9876500994")
    assert a["user_id"] is None and b["user_id"] is None
    assert a["id"] != b["id"]

    with Session(world["engine"]) as session:
        session.execute(delete(Member).where(Member.id.in_([a["id"], b["id"]])))
        session.commit()
