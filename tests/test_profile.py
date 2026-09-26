from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.main import app
from app.models.media import Media
from app.models.member import Member
from app.models.user import User


class _CdnStorage:
    """Storage backend that hands back a public CDN URL, like R2 does."""

    available = True
    serves_public_urls = True

    def __init__(self, url: str):
        self._url = url

    def upload_bytes(self, object_key, data, mime_type):
        return self._url

    def read_bytes(self, object_key):
        return b""

    def delete_object(self, object_key):
        return None


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _mock_firebase(monkeypatch):
    def verify_id_token(id_token: str) -> dict:
        return {"uid": f"profile-{id_token}", "phone_number": id_token}

    monkeypatch.setattr(
        "app.services.auth_service.firebase_verify_id_token", verify_id_token
    )


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


def _next_phone() -> str:
    return f"+916{str(time.time_ns())[-9:]}"


def _cleanup(phones: list[str]) -> None:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            user_ids = select(User.id).where(User.phone_number.in_(phones))
            connection.execute(delete(Media).where(Media.uploaded_by.in_(user_ids)))
            connection.execute(delete(User).where(User.phone_number.in_(phones)))
            connection.commit()
    finally:
        engine.dispose()


def _delete_members(phones: list[str]) -> None:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            user_ids = select(User.id).where(User.phone_number.in_(phones))
            connection.execute(delete(Member).where(Member.user_id.in_(user_ids)))
            connection.commit()
    finally:
        engine.dispose()


def _register(client: TestClient, phone: str) -> str:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Profile Test User",
            "email": None,
            "password": "V3ryStr0ng!Pass",
            "phone_number": phone.removeprefix("+91"),
            "confirm_password": "V3ryStr0ng!Pass",
            "id_token": phone,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["tokens"]["access_token"]


def test_profile_requires_authentication(client: TestClient):
    response = client.get("/api/v1/profile")
    assert response.status_code == 401


def test_profile_get_returns_the_complete_shape(client: TestClient):
    phone = _next_phone()
    try:
        token = _register(client, phone)
        response = client.get(
            "/api/v1/profile", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True
        assert set(body["profile"]) == {
            "full_name",
            "member_dob",
            "member_photo_link",
            "spouse_name",
            "spouse_mobile",
            "spouse_dob",
            "spouse_photo_link",
            "spouse_education",
            "spouse_occupation",
            "anniversary_date",
            "family_member_count",
            "son_name",
            "son_dob",
            "son_education",
            "son_occupation",
            "daughter_name",
            "daughter_dob",
            "daughter_education",
            "daughter_occupation",
            "member_education",
            "member_occupation",
            "company_name",
            "group_designation",
            "social_group_name",
            "interest_fields",
            "address",
            "city",
            "area",
            "phone_number",
            "email",
        }
        assert body["profile"]["full_name"] == "Profile Test User"
    finally:
        _cleanup([phone])


def test_profile_update_is_partial_and_preserves_values(client: TestClient):
    phone = _next_phone()
    try:
        token = _register(client, phone)
        headers = {"Authorization": f"Bearer {token}"}
        update = client.put(
            "/api/v1/profile",
            headers=headers,
            json={
                "full_name": "Updated Profile User",
                "spouse_name": "Spouse User",
                "spouse_dob": "not-a-strict-date",
                "phone_number": "+91 99999 99999",
                "address": "Updated address",
            },
        )
        assert update.status_code == 200, update.text
        assert update.json() == {
            "success": True,
            "message": "Profile updated successfully",
        }

        profile = client.get("/api/v1/profile", headers=headers).json()["profile"]
        assert profile["full_name"] == "Updated Profile User"
        assert profile["spouse_name"] == "Spouse User"
        assert profile["spouse_dob"] == "not-a-strict-date"
        assert profile["phone_number"] == "+91 99999 99999"
        assert profile["address"] == "Updated address"
        assert profile["member_education"] is None

        clear = client.put(
            "/api/v1/profile", headers=headers, json={"email": None}
        )
        assert clear.status_code == 200, clear.text
        profile = client.get("/api/v1/profile", headers=headers).json()["profile"]
        assert profile["email"] is None
        assert profile["full_name"] == "Updated Profile User"
    finally:
        _cleanup([phone])


def test_profile_update_accepts_all_profile_fields(client: TestClient):
    phone = _next_phone()
    values = {
        "full_name": "Full Profile Name",
        "member_dob": "1980-01-02",
        "member_photo_link": "https://cdn.example.test/member.jpg",
        "spouse_name": "Spouse Name",
        "spouse_mobile": "+91 90000 00001",
        "spouse_dob": "1981-02-03",
        "spouse_photo_link": "https://cdn.example.test/spouse.jpg",
        "spouse_education": "Master's degree",
        "spouse_occupation": "Teacher",
        "anniversary_date": "2010-03-04",
        "family_member_count": "4",
        "son_name": "Son Name",
        "son_dob": "2010-04-05",
        "son_education": "Graduate",
        "son_occupation": "Engineer",
        "daughter_name": "Daughter Name",
        "daughter_dob": "2012-05-06",
        "daughter_education": "Graduate",
        "daughter_occupation": "Doctor",
        "member_education": "Bachelor's degree",
        "member_occupation": "Business",
        "company_name": "Example Company",
        "group_designation": "Secretary",
        "social_group_name": "Example Group",
        "interest_fields": "Music, travel",
        "address": "Example address",
        "city": "Example City",
        "area": "Example Area",
        "phone_number": "+91 90000 00002",
        "email": "profile@example.test",
    }
    try:
        token = _register(client, phone)
        headers = {"Authorization": f"Bearer {token}"}
        response = client.put("/api/v1/profile", headers=headers, json=values)
        assert response.status_code == 200, response.text
        profile = client.get("/api/v1/profile", headers=headers).json()["profile"]
        assert profile == values
    finally:
        _cleanup([phone])


def test_profile_accepts_long_values_without_column_overflow(client: TestClient):
    phone = _next_phone()
    values = {
        "full_name": "N" * 300,
        "email": "e" * 300,
        "phone_number": "+91 " + "9" * 40,
        "spouse_name": "S" * 300,
        "spouse_mobile": "+91 " + "8" * 40,
        "company_name": "C" * 300,
    }
    try:
        token = _register(client, phone)
        headers = {"Authorization": f"Bearer {token}"}
        response = client.put("/api/v1/profile", headers=headers, json=values)
        assert response.status_code == 200, response.text
        profile = client.get("/api/v1/profile", headers=headers).json()["profile"]
        for field, value in values.items():
            assert profile[field] == value
    finally:
        _cleanup([phone])


def test_profile_returns_not_found_without_member(client: TestClient):
    phone = _next_phone()
    try:
        token = _register(client, phone)
        _delete_members([phone])
        response = client.get(
            "/api/v1/profile", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 404
        assert response.json() == {
            "success": False,
            "message": "प्रोफाइल नहीं मिला",
            "error_code": "PROFILE_NOT_FOUND",
        }
    finally:
        _cleanup([phone])


def test_profile_update_returns_not_found_without_member(client: TestClient):
    phone = _next_phone()
    try:
        token = _register(client, phone)
        _delete_members([phone])
        response = client.put(
            "/api/v1/profile",
            headers={"Authorization": f"Bearer {token}"},
            json={"full_name": "Should Not Persist"},
        )
        assert response.status_code == 404
        assert response.json() == {
            "success": False,
            "message": "प्रोफाइल नहीं मिला",
            "error_code": "PROFILE_NOT_FOUND",
        }
    finally:
        _cleanup([phone])


def test_profile_update_ignores_fields_that_are_not_sent(client: TestClient):
    phone = _next_phone()
    try:
        token = _register(client, phone)
        headers = {"Authorization": f"Bearer {token}"}
        first = client.put(
            "/api/v1/profile",
            headers=headers,
            json={"address": "Keep me", "spouse_name": "Keep spouse"},
        )
        assert first.status_code == 200, first.text

        # A payload touching one field must not null out the others.
        second = client.put(
            "/api/v1/profile",
            headers=headers,
            json={"phone_number": "+91 88888 88888"},
        )
        assert second.status_code == 200, second.text

        profile = client.get("/api/v1/profile", headers=headers).json()["profile"]
        assert profile["address"] == "Keep me"
        assert profile["spouse_name"] == "Keep spouse"
        assert profile["phone_number"] == "+91 88888 88888"
    finally:
        _cleanup([phone])


def test_profile_photo_rejects_invalid_type(client: TestClient):
    phone = _next_phone()
    try:
        token = _register(client, phone)
        response = client.post(
            "/api/v1/profile/photo",
            headers={"Authorization": f"Bearer {token}"},
            files={"photo": ("profile.gif", b"not-an-image", "image/gif")},
        )
        assert response.status_code == 400
        assert response.json() == {
            "success": False,
            "message": "गलत फोटो फ़ाइल",
        }
    finally:
        _cleanup([phone])


def test_profile_photo_uploads_and_updates_profile(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    phone = _next_phone()
    try:
        token = _register(client, phone)

        monkeypatch.setattr(
            "app.services.media_service.get_storage",
            lambda: _CdnStorage("https://cdn.example.test/profile-photo.jpg"),
        )
        response = client.post(
            "/api/v1/profile/photo",
            headers={"Authorization": f"Bearer {token}"},
            files={"photo": ("profile.jpg", b"jpeg-content", "image/jpeg")},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "success": True,
            "photo_link": "https://cdn.example.test/profile-photo.jpg",
        }
        profile = client.get(
            "/api/v1/profile", headers={"Authorization": f"Bearer {token}"}
        ).json()["profile"]
        assert profile["member_photo_link"] == "https://cdn.example.test/profile-photo.jpg"
    finally:
        _cleanup([phone])


def test_profile_photo_sanitizes_long_filenames(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    phone = _next_phone()
    try:
        token = _register(client, phone)

        monkeypatch.setattr(
            "app.services.media_service.get_storage",
            lambda: _CdnStorage("https://cdn.example.test/long-profile-photo.jpg"),
        )
        response = client.post(
            "/api/v1/profile/photo",
            headers={"Authorization": f"Bearer {token}"},
            files={"photo": (f"{'a' * 300}.jpg", b"jpeg-content", "image/jpeg")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["photo_link"] == (
            "https://cdn.example.test/long-profile-photo.jpg"
        )
    finally:
        _cleanup([phone])


def test_profile_photo_url_actually_serves_the_image(client: TestClient):
    """Regression guard: the stored photo_link must be a fetchable URL.

    Previously an unconfigured environment returned '/dev-media/<key>' - a path
    with no route behind it - so the photo_link was stored but never rendered.
    """
    phone = _next_phone()
    try:
        token = _register(client, phone)

        response = client.post(
            "/api/v1/profile/photo",
            headers={"Authorization": f"Bearer {token}"},
            files={"photo": ("profile.jpg", b"jpeg-content", "image/jpeg")},
        )
        assert response.status_code == 200, response.text
        photo_link = response.json()["photo_link"]
        assert photo_link.startswith("http://")
        assert "/api/v1/media/" in photo_link

        # No auth needed: profile photos are public media.
        content = client.get(photo_link[len("http://testserver") :])
        assert content.status_code == 200, content.text
        assert content.content == b"jpeg-content"
        assert content.headers["content-type"] == "image/jpeg"
        assert content.headers["x-content-type-options"] == "nosniff"

        profile = client.get(
            "/api/v1/profile", headers={"Authorization": f"Bearer {token}"}
        ).json()["profile"]
        assert profile["member_photo_link"] == photo_link
    finally:
        _cleanup([phone])



def test_profile_photo_rejects_files_over_five_megabytes(client: TestClient):
    phone = _next_phone()
    try:
        token = _register(client, phone)
        response = client.post(
            "/api/v1/profile/photo",
            headers={"Authorization": f"Bearer {token}"},
            files={
                "photo": (
                    "profile.jpg",
                    b"x" * (5 * 1024 * 1024 + 1),
                    "image/jpeg",
                )
            },
        )
        assert response.status_code == 400
        assert response.json()["message"] == "गलत फोटो फ़ाइल"
    finally:
        _cleanup([phone])
