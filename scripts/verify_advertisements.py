"""Verification script for Advertisement API functionality."""

import asyncio
import sys
import uuid
from decimal import Decimal
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import create_access_token
from app.database import async_session_factory, engine
from app.main import app
from app.models.advertisement import Advertisement
from app.models.group import SocialGroup
from app.models.user import Role, User

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n\x2d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


async def setup_test_users():
    async with async_session_factory() as session:
        super_role = (
            await session.execute(select(Role).where(Role.name == "SUPER_ADMIN"))
        ).scalar_one()
        group_role = (
            await session.execute(select(Role).where(Role.name == "GROUP_ADMIN"))
        ).scalar_one()

        group_a = SocialGroup(name=f"Verify Group A {uuid.uuid4().hex[:6]}")
        group_b = SocialGroup(name=f"Verify Group B {uuid.uuid4().hex[:6]}")
        session.add_all([group_a, group_b])
        await session.flush()

        root = User(
            phone_number=f"+918{uuid.uuid4().int % 1000000000:09d}",
            full_name="Super Admin User",
            password_hash="dummy_hash_for_test",
            is_active=True,
            is_phone_verified=True,
            role_id=super_role.id,
        )
        admin_a = User(
            phone_number=f"+918{uuid.uuid4().int % 1000000000:09d}",
            full_name="Group Admin User",
            password_hash="dummy_hash_for_test",
            is_active=True,
            is_phone_verified=True,
            role_id=group_role.id,
            group_id=group_a.id,
        )
        session.add_all([root, admin_a])
        await session.flush()
        await session.commit()

        res = {
            "group_a_id": str(group_a.id),
            "group_a_name": group_a.name,
            "group_b_id": str(group_b.id),
            "root_token": create_access_token(subject=str(root.id)),
            "admin_a_token": create_access_token(subject=str(admin_a.id)),
        }
    await engine.dispose()
    return res


def run_tests():
    data = asyncio.run(setup_test_users())
    root_auth = {"Authorization": f"Bearer {data['root_token']}"}
    admin_auth = {"Authorization": f"Bearer {data['admin_a_token']}"}

    print("--- 1. Testing GET /api/v1/advertisements/subscription-durations ---")
    with TestClient(app) as client:
        dur_res = client.get("/api/v1/advertisements/subscription-durations")
        assert dur_res.status_code == 200, f"Expected 200, got {dur_res.text}"
        durations = dur_res.json()["data"]
        print(f"Durations fetched: {len(durations)} options available.")
        assert any(d["months"] == 1 and d["is_default"] for d in durations), "Default 1-month missing"
        print("  ✓ Subscription durations dropdown options verified.")

        print("\n--- 2. Testing POST /api/v1/advertisements (Super Admin full payload) ---")
        payload = {
            "title": "Jain Business Expo 2026",
            "group_id": data["group_a_id"],
            "start_date": "2026-11-01T10:00:00Z",
            "end_date": "2026-12-01T10:00:00Z",
            "person_name": "Narendra Mehta",
            "mobile_number": "9819001122",
            "cover_image_url": "https://cdn.example.com/cover.jpg",
            "large_image_url": "https://cdn.example.com/large.jpg",
            "amount": 1000.00,
            "subscription_months": 1,
        }
        create_res = client.post("/api/v1/advertisements", headers=root_auth, json=payload)
        assert create_res.status_code in (200, 201), f"Create failed: {create_res.text}"
        ad1 = create_res.json()["data"]
        assert ad1["title"] == payload["title"]
        assert ad1["person_name"] == payload["person_name"]
        assert ad1["mobile_number"] == payload["mobile_number"]
        assert float(ad1["amount"]) == 1000.00
        assert ad1["subscription_months"] == 1
        assert ad1["is_active"] is True
        assert ad1["status"] == "APPROVED"
        assert ad1["group_name"] == data["group_a_name"]
        print(f"  ✓ Advertisement created with ID: {ad1['id']}, active: {ad1['is_active']}, group: {ad1['group_name']}")

        print("\n--- 3. Testing POST /api/v1/advertisements (Group Admin auto-group & auto end-date) ---")
        ga_payload = {
            "title": "Community Youth Workshop",
            "person_name": "Rohan Doshi",
            "mobile_number": "9820012345",
            "subscription_months": 3,
        }
        ga_res = client.post("/api/v1/advertisements", headers=admin_auth, json=ga_payload)
        assert ga_res.status_code in (200, 201), f"Group Admin create failed: {ga_res.text}"
        ad2 = ga_res.json()["data"]
        assert ad2["group_id"] == data["group_a_id"]
        assert ad2["group_name"] == data["group_a_name"]
        assert ad2["end_date"] is not None
        print(f"  ✓ Group admin ad auto-scoped to {ad2['group_name']} with 3 months end-date: {ad2['end_date']}")

        print("\n--- 4. Testing Group Admin security: cannot create ad for other group ---")
        foreign_payload = {
            "title": "Foreign Group Ad",
            "group_id": data["group_b_id"],
        }
        forbidden_res = client.post("/api/v1/advertisements", headers=admin_auth, json=foreign_payload)
        assert forbidden_res.status_code == 403, f"Expected 403, got {forbidden_res.status_code}"
        print("  ✓ Correctly rejected foreign group creation (403 Forbidden).")

        print("\n--- 5. Testing Image Upload Endpoints (cover & large) ---")
        cover_upload = client.post(
            f"/api/v1/advertisements/{ad1['id']}/cover-image",
            headers=root_auth,
            files={"file": ("cover.png", PNG_BYTES, "image/png")},
        )
        assert cover_upload.status_code in (200, 201), f"Cover upload failed: {cover_upload.text}"
        assert cover_upload.json()["data"]["cover_image_url"] is not None
        print(f"  ✓ Cover image uploaded: {cover_upload.json()['data']['cover_image_url']}")

        large_upload = client.post(
            f"/api/v1/advertisements/{ad1['id']}/large-image",
            headers=root_auth,
            files={"file": ("large.png", PNG_BYTES, "image/png")},
        )
        assert large_upload.status_code in (200, 201), f"Large upload failed: {large_upload.text}"
        assert large_upload.json()["data"]["large_image_url"] is not None
        print(f"  ✓ Large image uploaded: {large_upload.json()['data']['large_image_url']}")

        print("\n--- 6. Testing Cover Image Size Limit (Max 2MB) ---")
        oversized_cover = b"x" * (2500 * 1024)  # 2.5MB
        oversized_res = client.post(
            f"/api/v1/advertisements/{ad1['id']}/cover-image",
            headers=root_auth,
            files={"file": ("oversized.png", oversized_cover, "image/png")},
        )
        assert oversized_res.status_code == 400, f"Expected 400 for oversized cover, got {oversized_res.status_code}"
        print(f"  ✓ Oversized cover image rejected properly: {oversized_res.text}")

        print("\n--- 7. Testing PATCH /api/v1/advertisements/{id} ---")
        patch_res = client.patch(
            f"/api/v1/advertisements/{ad1['id']}",
            headers=root_auth,
            json={"title": "Updated Jain Expo 2026", "subscription_months": 6, "amount": 2500.00},
        )
        assert patch_res.status_code == 200, f"Patch failed: {patch_res.text}"
        updated = patch_res.json()["data"]
        assert updated["title"] == "Updated Jain Expo 2026"
        assert updated["subscription_months"] == 6
        assert float(updated["amount"]) == 2500.00
        print("  ✓ Advertisement updated successfully.")

        print("\n--- 8. Testing GET /api/v1/advertisements list ---")
        list_res = client.get("/api/v1/advertisements", headers=root_auth, params={"active_only": True})
        assert list_res.status_code == 200
        items = list_res.json()["data"]
        assert any(i["id"] == ad1["id"] for i in items)
        print(f"  ✓ Active advertisements listed: {len(items)} found.")

        print("\n--- 9. Testing DELETE /api/v1/advertisements/{id} ---")
        del_res = client.delete(f"/api/v1/advertisements/{ad1['id']}", headers=root_auth)
        assert del_res.status_code == 200
        get_res = client.get(f"/api/v1/advertisements/{ad1['id']}")
        assert get_res.status_code == 404
        print("  ✓ Advertisement deleted successfully.")

    print("\n==========================================")
    print("ALL 9 VERIFICATION CHECKS PASSED PERFECTLY!")
    print("==========================================")


if __name__ == "__main__":
    run_tests()
