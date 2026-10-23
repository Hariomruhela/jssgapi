"""Tests for the Jain Tirth directory API."""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models.group import SocialGroup
from app.models.jain_tirth_image import JainTirthImage
from app.models.media import Media, MediaObject
from app.models.user import Role, User

PASSWORD = "StrongPass123!"

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n\x2d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


def _next_phone() -> str:
    return f"+916{str(time.time_ns())[-9:]}"


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
            full_name=f"Tirth {role_name}",
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


def _login(client: TestClient, phone: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["tokens"]["access_token"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(scope="module")
def world(client, engine):
    group_a = _create_group(engine, f"Tirth Group A {uuid.uuid4()}")
    group_b = _create_group(engine, f"Tirth Group B {uuid.uuid4()}")

    admin_a_phone = _next_phone()
    admin_b_phone = _next_phone()
    root_phone = _next_phone()

    admin_a = _create_user(engine, "GROUP_ADMIN", admin_a_phone, group_a.id)
    _create_user(engine, "GROUP_ADMIN", admin_b_phone, group_b.id)
    root = _create_user(engine, "SUPER_ADMIN", root_phone)

    return {
        "group_a": group_a.id,
        "group_b": group_b.id,
        "admin_a_id": admin_a.id,
        "root_id": root.id,
        "tokens": {
            "admin_a": _login(client, admin_a_phone),
            "admin_b": _login(client, admin_b_phone),
            "root": _login(client, root_phone),
        },
    }


def _payload(**overrides) -> dict:
    payload = {
        "title": "Palitana Tirth",
        "description": "A famous Jain pilgrimage site",
        "location": "Palitana, Gujarat",
    }
    payload.update(overrides)
    return payload


def _create_tirth(client, world, token: str, **overrides) -> dict:
    response = client.post(
        "/api/v1/jain-tirths",
        json=_payload(**overrides),
        headers=_auth(world["tokens"][token]),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_create_stores_location_and_drops_timing_fields(client, world):
    tirth = _create_tirth(client, world, "admin_a")

    assert tirth["location"] == "Palitana, Gujarat"
    assert tirth["status"] == "PUBLISHED"
    assert tirth["is_public"] is True
    for absent in (
        "title_hi",
        "venue_name",
        "venue_address",
        "event_date",
        "event_time",
        "starts_at",
        "ends_at",
        "event_type",
        "host_id",
        "max_attendees",
        "registration_required",
        "location_id",
    ):
        assert absent not in tirth


def test_list_and_search_by_location(client, world):
    tirth = _create_tirth(
        client,
        world,
        "admin_a",
        title="Shikharji",
        location="Giridih, Jharkhand",
    )

    response = client.get(
        "/api/v1/jain-tirths?search=Jharkhand",
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["data"]}
    assert tirth["id"] in ids


def test_status_filter_narrows_the_list(client, world):
    tirth = _create_tirth(
        client, world, "admin_a", title="Draft Tirth", status="DRAFT"
    )

    response = client.get(
        "/api/v1/jain-tirths?status=draft",
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["data"]}
    assert tirth["id"] in ids
    assert all(item["status"] == "DRAFT" for item in response.json()["data"])


def test_update_changes_title_location_and_status(client, world):
    tirth = _create_tirth(client, world, "admin_a")

    response = client.patch(
        f"/api/v1/jain-tirths/{tirth['id']}",
        json={
            "title": "Updated Tirth",
            "location": "Ujjain, Madhya Pradesh",
            "status": "DRAFT",
        },
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["title"] == "Updated Tirth"
    assert data["location"] == "Ujjain, Madhya Pradesh"
    assert data["status"] == "DRAFT"


def test_clearing_title_is_rejected(client, world):
    tirth = _create_tirth(client, world, "admin_a")

    response = client.patch(
        f"/api/v1/jain-tirths/{tirth['id']}",
        json={"title": None},
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 422


def test_another_group_cannot_see_the_tirth(client, world):
    tirth = _create_tirth(client, world, "admin_a")

    response = client.get(
        f"/api/v1/jain-tirths/{tirth['id']}",
        headers=_auth(world["tokens"]["admin_b"]),
    )

    assert response.status_code == 404


def test_a_member_may_not_create_a_tirth(client, world, engine):
    phone = _next_phone()
    _create_user(engine, "MEMBER", phone)
    token = _login(client, phone)

    response = client.post(
        "/api/v1/jain-tirths",
        json=_payload(),
        headers=_auth(token),
    )

    assert response.status_code == 403


def test_cover_image_upload_replace_and_remove(client, world):
    tirth = _create_tirth(client, world, "admin_a")

    first = client.post(
        f"/api/v1/jain-tirths/{tirth['id']}/cover",
        files={"file": ("cover.png", PNG_BYTES, "image/png")},
        headers=_auth(world["tokens"]["admin_a"]),
    )
    second = client.post(
        f"/api/v1/jain-tirths/{tirth['id']}/cover",
        files={"file": ("cover.png", PNG_BYTES, "image/png")},
        headers=_auth(world["tokens"]["admin_a"]),
    )
    removed = client.delete(
        f"/api/v1/jain-tirths/{tirth['id']}/cover",
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["data"]["cover_image_url"] != first.json()["data"][
        "cover_image_url"
    ]
    assert removed.status_code == 200
    assert removed.json()["data"]["cover_image_url"] is None


def test_gallery_upload_detail_and_delete(client, world):
    tirth = _create_tirth(client, world, "admin_a")

    uploaded = client.post(
        f"/api/v1/jain-tirths/{tirth['id']}/images",
        files=[
            ("files", ("one.png", PNG_BYTES, "image/png")),
            ("files", ("two.png", PNG_BYTES, "image/png")),
        ],
        headers=_auth(world["tokens"]["admin_a"]),
    )
    assert uploaded.status_code == 200, uploaded.text
    images = uploaded.json()["data"]
    assert [image["position"] for image in images] == [0, 1]

    detail = client.get(
        f"/api/v1/jain-tirths/{tirth['id']}",
        headers=_auth(world["tokens"]["admin_a"]),
    )
    assert detail.json()["data"]["image_count"] == 2
    assert len(detail.json()["data"]["images"]) == 2

    removed = client.delete(
        f"/api/v1/jain-tirths/{tirth['id']}/images/{images[0]['id']}",
        headers=_auth(world["tokens"]["admin_a"]),
    )
    assert removed.status_code == 200


def test_at_most_ten_images_are_allowed(client, world):
    tirth = _create_tirth(client, world, "admin_a")
    for _ in range(5):
        response = client.post(
            f"/api/v1/jain-tirths/{tirth['id']}/images",
            files=[
                ("files", ("one.png", PNG_BYTES, "image/png")),
                ("files", ("two.png", PNG_BYTES, "image/png")),
            ],
            headers=_auth(world["tokens"]["admin_a"]),
        )
        assert response.status_code == 200, response.text

    response = client.post(
        f"/api/v1/jain-tirths/{tirth['id']}/images",
        files=[("files", ("eleven.png", PNG_BYTES, "image/png"))],
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "TOO_MANY_IMAGES"


def test_delete_removes_gallery_and_media(client, world, engine):
    tirth = _create_tirth(client, world, "admin_a")
    client.post(
        f"/api/v1/jain-tirths/{tirth['id']}/images",
        files=[("files", ("one.png", PNG_BYTES, "image/png"))],
        headers=_auth(world["tokens"]["admin_a"]),
    )

    with engine.connect() as connection:
        object_keys = connection.execute(
            select(Media.r2_object_key).where(
                Media.owner_type == "jain_tirth",
                Media.owner_id == uuid.UUID(tirth["id"]),
            )
        ).scalars().all()

    deleted = client.delete(
        f"/api/v1/jain-tirths/{tirth['id']}",
        headers=_auth(world["tokens"]["root"]),
    )
    assert deleted.status_code == 200

    fetched = client.get(
        f"/api/v1/jain-tirths/{tirth['id']}",
        headers=_auth(world["tokens"]["admin_a"]),
    )
    assert fetched.status_code == 404

    with engine.connect() as connection:
        remaining = connection.execute(
            select(JainTirthImage).where(
                JainTirthImage.jain_tirth_id == uuid.UUID(tirth["id"]),
                JainTirthImage.is_deleted.is_(False),
            )
        ).scalars().all()
        assert remaining == []
        blobs = connection.execute(
            select(MediaObject.object_key).where(
                MediaObject.object_key.in_(object_keys)
            )
        ).scalars().all()
        assert blobs == []


def test_the_tables_have_the_expected_columns(client, world, engine):
    with engine.connect() as connection:
        inspector = inspect(connection)
        tirth_columns = {
            column["name"] for column in inspector.get_columns("jain_tirths")
        }
        image_columns = {
            column["name"]
            for column in inspector.get_columns("jain_tirth_images")
        }

    assert {
        "title",
        "description",
        "location",
        "cover_image_url",
        "cover_image_object_key",
        "status",
        "is_public",
        "group_id",
    } <= tirth_columns
    assert "title_hi" not in tirth_columns
    assert {
        "event_date",
        "event_time",
        "starts_at",
        "ends_at",
        "venue_name",
        "venue_address",
        "location_id",
    }.isdisjoint(tirth_columns)
    assert {"jain_tirth_id", "media_id", "url", "position"} <= image_columns
