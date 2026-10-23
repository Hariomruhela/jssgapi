"""Creating a group can also create its default Group Admin.

The admin is supplied as a name/phone pair on ``POST /groups``. A new phone gets
an account with an unknowable password (OTP login), while an existing account is
promoted instead of duplicated.
"""

from __future__ import annotations

import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models.group import SocialGroup
from app.models.user import Role, User

PASSWORD = "StrongPass123!"
INDORE = {"state": "Madhya Pradesh", "city": "Indore"}

_created_groups: list[str] = []
_created_phones: list[str] = []


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


def _next_phone() -> str:
    return f"+917{str(time.time_ns())[-9:]}"


def _role_id(session: Session, role_name: str) -> UUID:
    return session.execute(select(Role.id).where(Role.name == role_name)).scalar_one()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def auth(client: TestClient) -> dict:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    phone = _next_phone()
    try:
        with Session(engine) as session:
            session.add(
                User(
                    phone_number=phone,
                    password_hash=hash_password(PASSWORD),
                    full_name="Group Admin Creator",
                    is_email_verified=True,
                    is_phone_verified=True,
                    role_id=_role_id(session, "SUPER_ADMIN"),
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
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        with Session(engine) as session:
            session.execute(
                delete(SocialGroup).where(
                    SocialGroup.id.in_([UUID(g) for g in _created_groups])
                )
            )
            session.execute(delete(User).where(User.phone_number.in_(_created_phones)))
            session.commit()
    finally:
        engine.dispose()


def _create_group(client: TestClient, auth: dict, **extra) -> dict:
    response = client.post(
        "/api/v1/groups",
        json={"name": f"Admin Group {uuid4().hex[:8]}", **INDORE, **extra},
        headers=auth,
    )
    assert response.status_code in (200, 201), response.text
    group = response.json()["data"]
    _created_groups.append(group["id"])
    return group


def test_group_create_with_admin_creates_default_group_admin(client, auth):
    phone = _next_phone()
    _created_phones.append(phone)

    group = _create_group(
        client,
        auth,
        admin_name="Ravi Kumar",
        admin_phone_number=phone,
    )

    assert len(group["admins"]) == 1, group
    admin = group["admins"][0]
    assert admin["full_name"] == "Ravi Kumar"
    assert admin["phone_number"] == phone

    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        with Session(engine) as session:
            user = session.execute(
                select(User).where(User.phone_number == phone)
            ).scalar_one()
            assert str(user.id) == admin["id"]
            assert user.group_id == UUID(group["id"])
            assert user.role.name == "GROUP_ADMIN"
    finally:
        engine.dispose()


def test_group_create_reuses_an_existing_account_as_admin(client, auth):
    local_phone = _next_phone().removeprefix("+91")
    _created_phones.append(local_phone)
    created_phone = f"+91{local_phone}"
    _created_phones.append(created_phone)

    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        with Session(engine) as session:
            existing = User(
                phone_number=local_phone,
                password_hash=hash_password(PASSWORD),
                full_name="Existing Member",
                is_active=True,
                is_email_verified=True,
                is_phone_verified=True,
                role_id=_role_id(session, "MEMBER"),
            )
            session.add(existing)
            session.commit()
            existing_id = str(existing.id)
    finally:
        engine.dispose()

    group = _create_group(
        client,
        auth,
        admin_name="Entered Name",
        admin_phone_number=local_phone,
    )

    assert [a["id"] for a in group["admins"]] == [existing_id]
    assert group["admins"][0]["full_name"] == "Existing Member"

    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        with Session(engine) as session:
            user = session.execute(
                select(User).where(User.phone_number == local_phone)
            ).scalar_one()
            assert user.role.name == "GROUP_ADMIN"
            assert user.group_id == UUID(group["id"])
            assert (
                session.execute(
                    select(User).where(User.phone_number == created_phone)
                ).scalar_one_or_none()
                is None
            )
    finally:
        engine.dispose()


def test_group_create_requires_admin_name_and_phone_together(client, auth):
    base = {"name": f"Admin Pair {uuid4().hex[:8]}", **INDORE}
    name_only = client.post(
        "/api/v1/groups",
        json={**base, "admin_name": "Ravi Kumar"},
        headers=auth,
    )
    assert name_only.status_code == 422, name_only.text

    phone_only = client.post(
        "/api/v1/groups",
        json={**base, "admin_phone_number": _next_phone()},
        headers=auth,
    )
    assert phone_only.status_code == 422, phone_only.text


def test_group_create_rejects_an_invalid_admin_phone(client, auth):
    response = client.post(
        "/api/v1/groups",
        json={
            "name": f"Admin Bad Phone {uuid4().hex[:8]}",
            **INDORE,
            "admin_name": "Ravi Kumar",
            "admin_phone_number": "12345",
        },
        headers=auth,
    )
    assert response.status_code == 422, response.text


def test_existing_group_get_admin_when_reposted_with_admin(client, auth):
    name = f"Admin Existing {uuid4().hex[:8]}"
    first = client.post("/api/v1/groups", json={"name": name, **INDORE}, headers=auth)
    assert first.status_code in (200, 201), first.text
    group_id = first.json()["data"]["id"]
    _created_groups.append(group_id)

    phone = _next_phone()
    _created_phones.append(phone)
    second = client.post(
        "/api/v1/groups",
        json={
            "name": name,
            **INDORE,
            "admin_name": "Ravi Kumar",
            "admin_phone_number": phone,
        },
        headers=auth,
    )
    assert second.status_code in (200, 201), second.text
    group = second.json()["data"]
    assert group["id"] == group_id
    assert [a["phone_number"] for a in group["admins"]] == [phone]
