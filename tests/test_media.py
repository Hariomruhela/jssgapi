from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.pool import NullPool

from app.core.exceptions import ServiceUnavailableException
from app.main import app
from app.models.media import Media, MediaObject
from app.models.user import Permission, Role, RolePermission, User
from app.services.media_storage import (
    CloudflareR2Client,
    DatabaseStorage,
    LocalDiskStorage,
    StorageBackend,
    get_storage,
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
            # The Postgres backend is keyed independently of the media row, so
            # the blobs have to go too or the test database grows forever.
            object_keys = select(Media.r2_object_key).where(
                Media.uploaded_by.in_(user_ids)
            )
            connection.execute(
                delete(MediaObject).where(MediaObject.object_key.in_(object_keys))
            )
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


def _clear_r2(monkeypatch: pytest.MonkeyPatch) -> None:
    for field in (
        "cloudflare_r2_endpoint",
        "cloudflare_r2_access_key",
        "cloudflare_r2_secret_key",
        "cloudflare_r2_bucket",
        "cloudflare_r2_public_url",
    ):
        monkeypatch.setattr(f"app.services.media_storage.settings.{field}", "")


def test_database_storage_round_trip():
    """Bytes written to Postgres come back byte for byte."""
    storage = DatabaseStorage()
    key = f"images/test/{uuid.uuid4()}.jpg"
    # A leading backslash is the classic bytea-read-as-text corruption case.
    payload = b"\xff\xd8\x00\\binary\x00payload"

    try:
        assert storage.upload_bytes(key, payload, "image/jpeg") == ""
        assert storage.read_bytes(key) == payload
    finally:
        storage.delete_object(key)

    with pytest.raises(ServiceUnavailableException):
        storage.read_bytes(key)


def test_database_storage_overwrites_an_existing_key():
    storage = DatabaseStorage()
    key = f"images/test/{uuid.uuid4()}.jpg"
    try:
        storage.upload_bytes(key, b"first", "image/jpeg")
        storage.upload_bytes(key, b"second-longer", "image/png")
        assert storage.read_bytes(key) == b"second-longer"
    finally:
        storage.delete_object(key)


def test_database_storage_rejects_traversal_keys():
    """Keys must behave the same on both backends.

    ``..`` is rejected outright and a leading slash is normalised rather than
    treated as absolute, matching ``LocalDiskStorage`` so an object key stays
    portable between backends.
    """
    storage = DatabaseStorage()
    for bad in ("../../escape.jpg", "a/../../escape.jpg", "../" * 6 + "escape.jpg"):
        with pytest.raises(ServiceUnavailableException):
            storage.upload_bytes(bad, b"nope", "image/jpeg")

    normalised = f"etc/test/{uuid.uuid4()}.bin"
    try:
        assert storage.upload_bytes(f"/{normalised}", b"nope", "image/jpeg") == ""
        assert storage.read_bytes(normalised) == b"nope"
    finally:
        storage.delete_object(normalised)


def test_database_storage_never_claims_a_public_url():
    assert DatabaseStorage.serves_public_urls is False


def test_bytes_outlive_the_instance_that_wrote_them():
    """A cold start must not lose the file.

    Local disk failed exactly this: the upload handler wrote the bytes and
    returned 200, and a later request on a different instance - or the same
    instance after a cold start - found nothing there. Reading through a
    separate backend object with a fresh engine models that second request.
    """
    writer = DatabaseStorage()
    reader = DatabaseStorage()
    reader._engine = None
    key = f"images/test/{uuid.uuid4()}.png"
    payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    try:
        writer.upload_bytes(key, payload, "image/png")
        assert DatabaseStorage().read_bytes(key) == payload
    finally:
        writer.delete_object(key)


def test_auto_backend_uses_postgres_when_r2_is_absent(monkeypatch):
    """The regression this backend exists for.

    Falling back to local disk on a serverless host loses the file on the next
    cold start, so an unconfigured R2 must land on Postgres instead.
    """
    _clear_r2(monkeypatch)
    monkeypatch.setattr(
        "app.services.media_storage.settings.media_storage_backend", "auto"
    )
    assert isinstance(get_storage(), DatabaseStorage)


def test_auto_backend_prefers_r2_when_fully_configured(monkeypatch):
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
        "app.services.media_storage.settings.media_storage_backend", "auto"
    )
    assert isinstance(get_storage(), CloudflareR2Client)


def test_local_backend_is_opt_in(monkeypatch):
    _clear_r2(monkeypatch)
    monkeypatch.setattr(
        "app.services.media_storage.settings.media_storage_backend", "local"
    )
    assert isinstance(get_storage(), LocalDiskStorage)


def test_explicit_r2_backend_is_honoured_even_when_unconfigured(monkeypatch):
    """An operator asking for R2 must get R2, so calls fail loudly."""
    _clear_r2(monkeypatch)
    monkeypatch.setattr(
        "app.services.media_storage.settings.media_storage_backend", "r2"
    )
    storage = get_storage()
    assert isinstance(storage, CloudflareR2Client)
    assert storage.available is False


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


def _grant_media_upload(phone: str) -> None:
    """Attach a role that holds media.upload to a freshly registered user."""
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        with engine.begin() as connection:
            role_id = connection.execute(
                select(Role.id)
                .join(RolePermission, RolePermission.role_id == Role.id)
                .join(Permission, Permission.id == RolePermission.permission_id)
                .where(Permission.name == "media.upload", Role.name == "ADMIN")
            ).scalar_one()
            connection.execute(
                User.__table__.update()
                .where(User.phone_number == phone)
                .values(role_id=role_id)
            )
    finally:
        engine.dispose()


def test_media_content_404_for_unknown_id(client: TestClient):
    response = client.get(f"/api/v1/media/{uuid.uuid4()}/content")
    assert response.status_code == 404, response.text


def test_upload_endpoint_returns_a_serialisable_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Regression guard for a 500 on every successful upload.

    created_at/updated_at are server defaults, so the row is expired after the
    flush. Serialising it then triggered an implicit lazy load, which cannot be
    awaited, and the endpoint answered 500 - after the bytes were already
    stored. Only the failure paths were covered before.
    """
    phone = _next_phone()
    try:
        token = _register(client, phone)
        _grant_media_upload(phone)
        monkeypatch.setattr(
            "app.services.media_service.get_storage", lambda: DatabaseStorage()
        )
        response = client.post(
            "/api/v1/media/upload",
            headers={"Authorization": f"Bearer {token}"},
            data={"owner_type": "member", "owner_id": str(uuid.uuid4())},
            files={"file": ("photo.jpg", b"jpeg-content", "image/jpeg")},
        )
        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["file_size"] == len(b"jpeg-content")
        assert body["created_at"] and body["updated_at"]

        fetched = client.get(
            f"/api/v1/media/{body['id']}/content",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert fetched.status_code == 200, fetched.text
        assert fetched.content == b"jpeg-content"
    finally:
        _cleanup([phone])


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
