from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.core.exceptions import ServiceUnavailableException
from app.main import app
from app.models.media import Media, MediaObject
from app.models.user import Permission, Role, RolePermission, User
from app.services.media_storage import (
    CloudflareR2Client,
    DatabaseStorage,
    LocalDiskStorage,
    StorageBackend,
    _r2_credential_problems,
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

    def object_exists(self, object_key):
        return False

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


# --- Cloudflare R2 signing configuration -------------------------------------
#
# SignatureDoesNotMatch only says "these credentials are not a pair R2 accepts".
# Each test below pins one way that can be true, so the mistake is reported by
# name at startup instead of surfacing as an opaque 500 on the first read.


def _configure_r2(
    monkeypatch: pytest.MonkeyPatch,
    *,
    endpoint: str = "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
    access_key: str = "0123456789abcdef0123456789abcdef",
    secret_key: str = "a" * 64,
    bucket: str = "jssg-media",
    public_url: str = "https://pub-3fe198c7c5a241e68f2f0c33419856b6.r2.dev",
) -> CloudflareR2Client:
    monkeypatch.setattr(
        "app.services.media_storage.settings.cloudflare_r2_endpoint", endpoint
    )
    monkeypatch.setattr(
        "app.services.media_storage.settings.cloudflare_r2_access_key", access_key
    )
    monkeypatch.setattr(
        "app.services.media_storage.settings.cloudflare_r2_secret_key", secret_key
    )
    monkeypatch.setattr(
        "app.services.media_storage.settings.cloudflare_r2_bucket", bucket
    )
    monkeypatch.setattr(
        "app.services.media_storage.settings.cloudflare_r2_public_url", public_url
    )
    return CloudflareR2Client()


@pytest.mark.parametrize(
    "endpoint",
    [
        pytest.param(
            "https://pub-3fe198c7c5a241e68f2f0c33419856b6.r2.dev", id="r2.dev"
        ),
        pytest.param("https://media.example.test", id="custom-public-domain"),
        pytest.param("http://localhost:9000", id="minio-localhost"),
        pytest.param("https://jssg-media.s3.amazonaws.com", id="aws-s3"),
        pytest.param("https://r2.cloudflarestorage.com", id="missing-account-id"),
        pytest.param("https://acct.r2.cloudflarestorage.com/media", id="with-path"),
    ],
)
def test_r2_rejects_an_endpoint_that_is_not_the_s3_api(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
):
    """Only the API host can accept a signed request.

    A public URL or a local endpoint is answered with a signature error, which
    blames the credentials, so it has to be rejected before the first request.
    """
    r2 = _configure_r2(monkeypatch, endpoint=endpoint)

    assert r2.available is False
    assert "CLOUDFLARE_R2_ENDPOINT" in r2.configuration_error


def test_r2_reports_missing_settings_by_variable_name(monkeypatch):
    _clear_r2(monkeypatch)

    r2 = CloudflareR2Client()

    assert r2.available is False
    assert set(r2.missing) == {
        "CLOUDFLARE_R2_ENDPOINT",
        "CLOUDFLARE_R2_ACCESS_KEY",
        "CLOUDFLARE_R2_SECRET_KEY",
        "CLOUDFLARE_R2_BUCKET",
    }
    for name in r2.missing:
        assert name in r2.unavailable_message


def test_r2_strips_pasted_whitespace_and_quotes_from_settings():
    """A newline or a pair of quotes around the value breaks SigV4.

    The secret is signed byte for byte, so an invisible trailing character
    turns a correct key into SignatureDoesNotMatch.
    """
    settings = Settings(
        _env_file=None,
        cloudflare_r2_endpoint=(
            "  https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com\n"
        ),
        cloudflare_r2_access_key='  "0123456789abcdef0123456789abcdef" ',
        cloudflare_r2_secret_key="a" * 64 + " \n",
        cloudflare_r2_bucket=" jssg-media ",
    )

    assert settings.cloudflare_r2_endpoint.endswith(".r2.cloudflarestorage.com")
    assert "\n" not in settings.cloudflare_r2_endpoint
    assert settings.cloudflare_r2_access_key == "0123456789abcdef0123456789abcdef"
    assert settings.cloudflare_r2_secret_key == "a" * 64
    assert settings.cloudflare_r2_bucket == "jssg-media"


def test_r2_names_the_wrong_kind_of_credential(monkeypatch):
    """API tokens, global keys and account ids are diagnosed, not guessed at."""
    account_id = "0123456789abcdef0123456789abcdef"
    endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    access_key = account_id

    _configure_r2(
        monkeypatch, endpoint=endpoint, access_key=access_key, secret_key="b" * 37
    )

    problems = " ".join(_r2_credential_problems(access_key, "b" * 37, endpoint))
    assert "account ID" in problems
    assert "Global API key" in problems

    token = "v1.0-" + "c" * 40
    token_problems = _r2_credential_problems(access_key, token, endpoint)
    assert any("API token" in problem for problem in token_problems)


def test_r2_credential_diagnostics_never_include_the_secret(monkeypatch):
    secret = "s3cr3t-" + "z" * 58
    endpoint = "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"
    _configure_r2(monkeypatch, endpoint=endpoint, secret_key=secret)

    problems = _r2_credential_problems(
        "0123456789abcdef0123456789abcdef", secret, endpoint
    )
    joined = " ".join(problems)
    assert problems  # the value is flagged
    assert secret not in joined
    assert secret[:16] not in joined


def test_r2_client_pins_sigv4_and_the_account_endpoint(monkeypatch):
    """PUT, GET and DELETE must all sign the same way.

    An ambient AWS_* variable or an ~/.aws file must not be able to change the
    region or the credentials a request is signed with.
    """
    endpoint = "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"
    access_key = "0123456789abcdef0123456789abcdef"
    secret_key = "a" * 64
    r2 = _configure_r2(
        monkeypatch,
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
    )

    recorded: dict = {}

    class _FakeClient:
        def __init__(self, service, **kwargs):
            recorded["service"] = service
            recorded.update(kwargs)

    class _FakeSession:
        def __init__(self, **kwargs):
            recorded["session"] = kwargs

        def client(self, service, **kwargs):
            return _FakeClient(service, **kwargs)

    import boto3

    monkeypatch.setattr(boto3.session, "Session", _FakeSession)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")

    client = r2._client()

    assert recorded["service"] == "s3"
    assert recorded["endpoint_url"] == endpoint
    assert recorded["session"] == {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "region_name": "auto",
    }
    assert recorded["config"].signature_version == "s3v4"
    assert recorded["config"].s3["addressing_style"] == "path"
    # One client per instance, so a single request cannot mix configurations.
    assert r2._client() is client


def test_r2_operations_fail_before_signing_when_unconfigured(monkeypatch):
    _clear_r2(monkeypatch)
    r2 = CloudflareR2Client()

    # No client is built, so nothing is signed against a broken configuration.
    for call in (
        lambda: r2.upload_bytes("image/x.jpg", b"bytes", "image/jpeg"),
        lambda: r2.read_bytes("image/x.jpg"),
    ):
        with pytest.raises(ServiceUnavailableException):
            call()


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
                .where(Permission.name == "media.upload", Role.name == "SUPER_ADMIN")
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
