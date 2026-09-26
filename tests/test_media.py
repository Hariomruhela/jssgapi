from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.pool import NullPool

from app.core.exceptions import ServiceUnavailableException
from app.main import app
from app.models.media import Media
from app.models.user import User
from app.services.media_storage import (
    CloudflareR2Client,
    LocalDiskStorage,
    StorageBackend,
)
from tests.test_profile import _psycopg_url, _register


class _BrokenStorage(StorageBackend):
    """Backend that cannot persist - models an outage / missing credentials."""

    available = False
    serves_public_urls = False

    def upload_bytes(self, object_key, data, mime_type):
        raise ServiceUnavailableException(
            "Media storage is not available", "MEDIA_STORAGE_UNAVAILABLE"
        )

    def read_bytes(self, object_key):
        raise ServiceUnavailableException(
            "Media storage is not available", "MEDIA_STORAGE_UNAVAILABLE"
        )

    def delete_object(self, object_key):
        return None


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _mock_firebase(monkeypatch):
    def verify_id_token(id_token: str) -> dict:
        return {"uid": f"media-{id_token}", "phone_number": id_token}

    monkeypatch.setattr(
        "app.services.auth_service.firebase_verify_id_token", verify_id_token
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


# --- storage layer ---------------------------------------------------------


def test_local_disk_storage_round_trip(tmp_path):
    storage = LocalDiskStorage(tmp_path)
    assert storage.upload_bytes("images/a/b.jpg", b"data", "image/jpeg") == ""
    assert storage.read_bytes("images/a/b.jpg") == b"data"
    storage.delete_object("images/a/b.jpg")
    with pytest.raises(ServiceUnavailableException):
        storage.read_bytes("images/a/b.jpg")


def test_local_disk_storage_cannot_escape_its_root(tmp_path):
    """Traversal keys must never write outside the storage root."""
    storage = LocalDiskStorage(tmp_path)
    sentinel = tmp_path.parent / "sentinel.txt"
    sentinel.write_text("original")

    bad_keys = (
        "../../sentinel.txt",
        "a/../../sentinel.txt",
        "../" * 6 + "sentinel.txt",
    )
    for bad in bad_keys:
        with pytest.raises(ServiceUnavailableException):
            storage.upload_bytes(bad, b"overwritten", "text/plain")

    assert sentinel.read_text() == "original"

    # A leading slash is normalised, not treated as an absolute path.
    storage.upload_bytes("/etc/passwd", b"harmless", "text/plain")
    assert (tmp_path / "etc" / "passwd").read_bytes() == b"harmless"
    assert sentinel.read_text() == "original"


def test_r2_does_not_claim_public_urls_without_public_url(monkeypatch):
    """Credentials present but no public domain configured.

    The backend must not hand back a CDN URL that does not exist.
    """
    monkeypatch.setattr(
        "app.services.media_storage.settings.cloudflare_r2_endpoint",
        "https://account.r2.cloudflarestorage.com",
    )
    monkeypatch.setattr(
        "app.services.media_storage.settings.cloudflare_r2_access_key", "k"
    )
    monkeypatch.setattr(
        "app.services.media_storage.settings.cloudflare_r2_secret_key", "s"
    )
    monkeypatch.setattr("app.services.media_storage.settings.cloudflare_r2_bucket", "b")
    monkeypatch.setattr(
        "app.services.media_storage.settings.cloudflare_r2_public_url", ""
    )

    r2 = CloudflareR2Client()
    assert r2.available is True
    assert r2.serves_public_urls is False


def test_r2_claims_public_urls_when_fully_configured(monkeypatch):
    monkeypatch.setattr(
        "app.services.media_storage.settings.cloudflare_r2_endpoint",
        "https://account.r2.cloudflarestorage.com",
    )
    monkeypatch.setattr(
        "app.services.media_storage.settings.cloudflare_r2_access_key", "k"
    )
    monkeypatch.setattr(
        "app.services.media_storage.settings.cloudflare_r2_secret_key", "s"
    )
    monkeypatch.setattr("app.services.media_storage.settings.cloudflare_r2_bucket", "b")
    monkeypatch.setattr(
        "app.services.media_storage.settings.cloudflare_r2_public_url",
        "https://media.example.test/",
    )
    r2 = CloudflareR2Client()
    assert r2.serves_public_urls is True


# --- API behaviour ---------------------------------------------------------


def test_upload_failure_returns_error_not_a_dead_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Core regression guard.

    A storage outage must surface as an error. Returning 200 with a URL that
    cannot be fetched is exactly what made profile photos disappear.
    """
    phone = _next_phone()
    try:
        token = _register(client, phone)
        monkeypatch.setattr(
            "app.services.media_service.get_storage", lambda: _BrokenStorage()
        )
        response = client.post(
            "/api/v1/profile/photo",
            headers={"Authorization": f"Bearer {token}"},
            files={"photo": ("profile.jpg", b"jpeg-content", "image/jpeg")},
        )
        assert response.status_code == 503, response.text
        assert response.json()["success"] is False
    finally:
        _cleanup([phone])


def test_empty_upload_is_rejected(client: TestClient):
    phone = _next_phone()
    try:
        token = _register(client, phone)
        response = client.post(
            "/api/v1/profile/photo",
            headers={"Authorization": f"Bearer {token}"},
            files={"photo": ("profile.jpg", b"", "image/jpeg")},
        )
        assert response.status_code in (400, 422), response.text
    finally:
        _cleanup([phone])


def test_media_content_404_for_unknown_id(client: TestClient):
    response = client.get(f"/api/v1/media/{uuid.uuid4()}/content")
    assert response.status_code == 404, response.text


def test_private_media_is_not_served_anonymously(client: TestClient):
    phone = _next_phone()
    try:
        token = _register(client, phone)
        media_id = _insert_private_media(phone)
        anonymous = client.get(f"/api/v1/media/{media_id}/content")
        assert anonymous.status_code in (401, 403), anonymous.text

        # The owner passes the access check. The row has no object behind it in
        # this test, so storage reports it missing - the point is that access
        # control no longer rejects them.
        owner = client.get(
            f"/api/v1/media/{media_id}/content",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert owner.status_code not in (401, 403), owner.text
    finally:
        _cleanup([phone])


def _insert_private_media(phone: str) -> uuid.UUID:
    """Insert a private Media row directly so no upload path is involved."""
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    media_id = uuid.uuid4()
    try:
        with engine.connect() as connection:
            user_id = connection.execute(
                select(User.id).where(User.phone_number == phone)
            ).scalar_one()
            connection.execute(
                Media.__table__.insert().values(
                    id=media_id,
                    owner_type="member",
                    owner_id=user_id,
                    file_name="secret.jpg",
                    file_type="image",
                    mime_type="image/jpeg",
                    file_size=4,
                    r2_object_key=f"images/{media_id}.jpg",
                    url=f"/api/v1/media/{media_id}/content",
                    is_public=False,
                    uploaded_by=user_id,
                )
            )
            connection.commit()
    finally:
        engine.dispose()
    return media_id
