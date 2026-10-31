"""Tests for the Advertisement API."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.database import engine as async_db_engine
from app.main import app
from app.models.advertisement import Advertisement
from app.models.group import SocialGroup
from app.models.user import Role, User

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n\x2d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
async def seed_data():
    from app.database import async_session_factory

    async with async_session_factory() as session:
        # Get or create super admin
        super_role = (
            await session.execute(select(Role).where(Role.name == "SUPER_ADMIN"))
        ).scalar_one()
        group_role = (
            await session.execute(select(Role).where(Role.name == "GROUP_ADMIN"))
        ).scalar_one()

        group_a = SocialGroup(name=f"Ad Group A {uuid.uuid4().hex[:6]}")
        group_b = SocialGroup(name=f"Ad Group B {uuid.uuid4().hex[:6]}")
        session.add_all([group_a, group_b])
        await session.flush()

        root = User(
            phone_number=f"+919{uuid.uuid4().int % 1000000000:09d}",
            full_name="Super Admin User",
            is_active=True,
            is_phone_verified=True,
            role_id=super_role.id,
        )
        admin_a = User(
            phone_number=f"+919{uuid.uuid4().int % 1000000000:09d}",
            full_name="Group Admin A",
            is_active=True,
            is_phone_verified=True,
            role_id=group_role.id,
            group_id=group_a.id,
        )
        session.add_all([root, admin_a])
        await session.flush()
        await session.commit()

        return {
            "group_a_id": group_a.id,
            "group_a_name": group_a.name,
            "group_b_id": group_b.id,
            "root_token": create_access_token(subject=str(root.id)),
            "admin_a_token": create_access_token(subject=str(admin_a.id)),
        }


def test_subscription_durations(client):
    """Verify subscription duration options returned for dropdown."""
    response = client.get("/api/v1/advertisements/subscription-durations")
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) >= 4
    months_list = [item["months"] for item in data]
    assert 1 in months_list
    assert 3 in months_list
    assert 6 in months_list
    assert 12 in months_list
    default_opt = next(item for item in data if item["months"] == 1)
    assert default_opt["is_default"] is True
    assert "1 Month" in default_opt["label"]


@pytest.mark.asyncio
async def test_create_advertisement_json_super_admin(client, seed_data):
    """Super admin creates an advertisement with all screenshot fields."""
    seed = await seed_data
    token = seed["root_token"]
    payload = {
        "title": "Grand Jewellery Diwali Offer",
        "group_id": str(seed["group_a_id"]),
        "start_date": "2026-11-01T00:00:00Z",
        "end_date": "2026-12-01T00:00:00Z",
        "person_name": "Rajesh Shah",
        "mobile_number": "9876543210",
        "cover_image_url": "https://example.com/cover.jpg",
        "large_image_url": "https://example.com/large.jpg",
        "amount": 1500.00,
        "subscription_months": 1,
    }
    response = client.post(
        "/api/v1/advertisements",
        headers=_auth(token),
        json=payload,
    )
    assert response.status_code == 201, response.text
    ad = response.json()["data"]
    assert ad["title"] == payload["title"]
    assert ad["person_name"] == payload["person_name"]
    assert ad["mobile_number"] == payload["mobile_number"]
    assert ad["cover_image_url"] == payload["cover_image_url"]
    assert ad["large_image_url"] == payload["large_image_url"]
    assert float(ad["amount"]) == 1500.00
    assert ad["subscription_months"] == 1
    assert ad["is_active"] is True
    assert ad["status"] == "APPROVED"
    assert ad["group_name"] == seed["group_a_name"]


@pytest.mark.asyncio
async def test_create_advertisement_group_admin_auto_group(client, seed_data):
    """Group admin creates an advertisement; group is auto-scoped to their group."""
    seed = await seed_data
    token = seed["admin_a_token"]
    payload = {
        "title": "Community Business Promotion",
        "person_name": "Suresh Jain",
        "mobile_number": "9822000000",
        "subscription_months": 3,
        "amount": 0,
    }
    response = client.post(
        "/api/v1/advertisements",
        headers=_auth(token),
        json=payload,
    )
    assert response.status_code == 201, response.text
    ad = response.json()["data"]
    assert ad["group_id"] == str(seed["group_a_id"])
    assert ad["group_name"] == seed["group_a_name"]
    assert ad["is_active"] is True
    assert ad["subscription_months"] == 3
    assert ad["end_date"] is not None


@pytest.mark.asyncio
async def test_group_admin_cannot_post_for_other_group(client, seed_data):
    """Group admin attempting to create ad for a foreign group is forbidden."""
    seed = await seed_data
    token = seed["admin_a_token"]
    payload = {
        "title": "Unauthorized Ad",
        "group_id": str(seed["group_b_id"]),
    }
    response = client.post(
        "/api/v1/advertisements",
        headers=_auth(token),
        json=payload,
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_upload_cover_and_large_images(client, seed_data):
    """Test image upload endpoints for cover image and large image."""
    seed = await seed_data
    token = seed["root_token"]
    create_res = client.post(
        "/api/v1/advertisements",
        headers=_auth(token),
        json={"title": "Ad With Uploaded Images"},
    )
    assert create_res.status_code == 201
    ad_id = create_res.json()["data"]["id"]

    # Upload cover image
    cover_res = client.post(
        f"/api/v1/advertisements/{ad_id}/cover-image",
        headers=_auth(token),
        files={"file": ("cover.png", PNG_BYTES, "image/png")},
    )
    assert cover_res.status_code == 201, cover_res.text
    assert cover_res.json()["data"]["cover_image_url"] is not None

    # Upload large image
    large_res = client.post(
        f"/api/v1/advertisements/{ad_id}/large-image",
        headers=_auth(token),
        files={"file": ("large.png", PNG_BYTES, "image/png")},
    )
    assert large_res.status_code == 201, large_res.text
    assert large_res.json()["data"]["large_image_url"] is not None

    # Delete cover image
    del_cover = client.delete(
        f"/api/v1/advertisements/{ad_id}/cover-image",
        headers=_auth(token),
    )
    assert del_cover.status_code == 200
    assert del_cover.json()["data"]["cover_image_url"] is None


@pytest.mark.asyncio
async def test_cover_image_max_size_enforcement(client, seed_data):
    """Cover image larger than 2MB is rejected."""
    seed = await seed_data
    token = seed["root_token"]
    create_res = client.post(
        "/api/v1/advertisements",
        headers=_auth(token),
        json={"title": "Size Test Ad"},
    )
    ad_id = create_res.json()["data"]["id"]

    # 2.5 MB content
    big_bytes = b"0" * (2500 * 1024)
    res = client.post(
        f"/api/v1/advertisements/{ad_id}/cover-image",
        headers=_auth(token),
        files={"file": ("big.png", big_bytes, "image/png")},
    )
    assert res.status_code == 400
    assert "2MB" in res.json().get("detail", {}).get("message", "")


@pytest.mark.asyncio
async def test_list_and_update_and_delete_advertisement(client, seed_data):
    """Test listing, updating, and deleting an advertisement."""
    seed = await seed_data
    token = seed["root_token"]
    create_res = client.post(
        "/api/v1/advertisements",
        headers=_auth(token),
        json={"title": "To Be Updated", "amount": 200},
    )
    ad_id = create_res.json()["data"]["id"]

    # Update
    patch_res = client.patch(
        f"/api/v1/advertisements/{ad_id}",
        headers=_auth(token),
        json={"title": "Updated Title", "amount": 500.00, "subscription_months": 6},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["data"]["title"] == "Updated Title"
    assert float(patch_res.json()["data"]["amount"]) == 500.00
    assert patch_res.json()["data"]["subscription_months"] == 6

    # List active
    list_res = client.get(
        "/api/v1/advertisements",
        headers=_auth(token),
        params={"active_only": True},
    )
    assert list_res.status_code == 200
    items = list_res.json()["data"]
    assert any(i["id"] == ad_id for i in items)

    # Delete
    del_res = client.delete(
        f"/api/v1/advertisements/{ad_id}",
        headers=_auth(token),
    )
    assert del_res.status_code == 200

    # Get should now return 404
    get_res = client.get(f"/api/v1/advertisements/{ad_id}")
    assert get_res.status_code == 404
