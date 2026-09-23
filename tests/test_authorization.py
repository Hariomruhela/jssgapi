from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

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
    suffix = str(int(time.time() * 1000))[-9:]
    return f"+916{suffix}"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_user(engine, role_name: str, phone: str) -> User:
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

    admin = _create_user(engine, "ADMIN", _next_phone())
    super_admin = _create_user(engine, "SUPER_ADMIN", _next_phone())
    group_admin = _create_user(engine, "GROUP_ADMIN", _next_phone())
    regional_admin = _create_user(engine, "REGIONAL_ADMIN", _next_phone())
    target_a = _create_user(engine, "MEMBER", _next_phone())
    target_b = _create_user(engine, "MEMBER", _next_phone())
    free_user = _create_user(engine, "MEMBER", _next_phone())
    role_target = _create_user(engine, "MEMBER", _next_phone())

    _create_member(engine, group_admin.id, group_a.id)
    _create_member(engine, regional_admin.id, group_a.id)
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
        "ADMIN": _login(client, admin.phone_number),
        "SUPER_ADMIN": _login(client, super_admin.phone_number),
        "GROUP_ADMIN": _login(client, group_admin.phone_number),
        "REGIONAL_ADMIN": _login(client, regional_admin.phone_number),
    }

    created = {
        "engine": engine,
        "group_a_id": group_a.id,
        "group_b_id": group_b.id,
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
            regional_admin.id,
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
        f"/api/v1/users/{world['member_user_id']}",
        json={"role_name": "ADMIN"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text

    resp = client.post(
        f"/api/v1/members/{world['member_a_id']}/approve",
        json={"status": "APPROVED"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text


def test_admin_role_allowed_like_super_admin(client, world):
    for role in ("ADMIN", "SUPER_ADMIN"):
        headers = _auth(world["tokens"][role])
        name = f"{role} Group {uuid.uuid4()}"
        resp = client.post("/api/v1/groups", json={"name": name}, headers=headers)
        assert resp.status_code in (200, 201), resp.text

        resp = client.get("/api/v1/users", headers=headers)
        assert resp.status_code == 200, resp.text


def test_admin_role_can_manage_user_roles(client, world):
    headers = _auth(world["tokens"]["ADMIN"])
    resp = client.patch(
        f"/api/v1/users/{world['role_target_id']}/role",
        json={"role_name": "ADMIN"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["role"]["name"] == "ADMIN"

    resp = client.patch(
        f"/api/v1/users/{world['role_target_id']}",
        json={"role_name": "GROUP_ADMIN"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["role"]["name"] == "GROUP_ADMIN"

    resp = client.patch(
        f"/api/v1/users/{world['role_target_id']}/role",
        json={"role_name": "MEMBER"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


def test_member_cannot_change_roles(client, world):
    headers = _auth(world["tokens"]["member"])
    resp = client.patch(
        f"/api/v1/users/{world['role_target_id']}/role",
        json={"role_name": "ADMIN"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text


def test_role_change_requires_authentication(client, world):
    resp = client.patch(
        f"/api/v1/users/{world['role_target_id']}/role",
        json={"role_name": "ADMIN"},
    )
    assert resp.status_code == 401, resp.text


def test_oauth2_token_endpoint_admin_only(client, world):
    engine = world["engine"]

    admin_phone = _next_phone()
    _create_user(engine, "ADMIN", admin_phone)

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


def test_signup_role_handling(client):
    engine = create_engine(_psycopg_url(), poolclass=NullPool)

    member_phone = _next_phone()
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "No Role",
            "email": f"norole_{int(time.time() * 1000)}@test.local",
            "password": PASSWORD,
            "id_token": member_phone,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["role"]["name"] == "MEMBER"

    admin_phone = _next_phone()
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Admin Signup",
            "email": f"adminsignup_{int(time.time() * 1000)}@test.local",
            "password": PASSWORD,
            "role": "Admin",
            "id_token": admin_phone,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["role"]["name"] == "ADMIN"

    empty_phone = _next_phone()
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Empty Role",
            "email": f"emptyrole_{int(time.time() * 1000)}@test.local",
            "password": PASSWORD,
            "role": "",
            "id_token": empty_phone,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["role"]["name"] == "MEMBER"

    bad_phone = _next_phone()
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Bad Role",
            "email": f"badrole_{int(time.time() * 1000)}@test.local",
            "password": PASSWORD,
            "role": "VIEWER",
            "id_token": bad_phone,
        },
    )
    assert resp.status_code == 400, resp.text

    _delete_users_by_phone(engine, [member_phone, admin_phone, empty_phone, bad_phone])
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
    _create_user(engine, "ADMIN", admin_phone)
    login = client.post(
        "/api/v1/auth/login",
        json={"phone_number": admin_phone, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["role"]["name"] == "ADMIN"

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
            "group_id": str(world["group_b_id"]),
        },
        headers=headers,
    )
    assert resp.status_code == 403, resp.text


def test_regional_admin_scoped_to_own_group(client, world):
    headers = _auth(world["tokens"]["REGIONAL_ADMIN"])

    resp = client.patch(
        f"/api/v1/members/{world['member_a_id']}",
        json={"first_name": "Carol"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    resp = client.patch(
        f"/api/v1/members/{world['member_b_id']}",
        json={"first_name": "Dan"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text

    resp = client.post(
        "/api/v1/events",
        json={
            "title": f"Regional event {uuid.uuid4()}",
            "starts_at": datetime.now(UTC).isoformat(),
            "group_id": str(world["group_b_id"]),
        },
        headers=headers,
    )
    assert resp.status_code == 403, resp.text
