"""Tests for the Google Drive to Cloudflare R2 member photo migration.

The migration exists because the member directory was seeded from a Google
Sheet whose photo column holds ``drive.google.com`` share links, which the
mobile app cannot render: an unshared file resolves to an
``accounts.google.com`` sign-in page and a shared one to Drive's HTML preview.
The bytes have to be copied somewhere that serves ``image/*`` unauthenticated.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.database import engine
from app.models.media import Media
from app.models.member import Member
from app.services.drive_photo_migration import DrivePhotoMigration
from app.services.google_drive_photos import (
    DownloadedPhoto,
    DrivePhotoError,
    PhotoLink,
    PhotoLinkKind,
    is_direct_image_url,
    is_drive_photo_url,
    parse_photo_link,
)
from app.services.media_storage import (
    CloudflareR2Client,
    LocalDiskStorage,
    StorageBackend,
)

# A valid 1x1 truecolour PNG, so the tests assert on real image bytes with real
# magic bytes and a correct IEND rather than on a stub.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90\x77\x53\xde"
    b"\x00\x00\x00\x0cIDATx\xda\x63\xe0\x16\x53\x04\x00\x00\x72\x00C\x0e\x34\xba&"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
JPEG_FILE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz234567"
DRIVE_LINK = f"https://drive.google.com/open?id={JPEG_FILE_ID}"
SPOUSE_FILE_ID = "1ZyXwVuTsRqPoNmLkJiHgFeDcBa09876"
SPOUSE_DRIVE_LINK = f"https://drive.google.com/file/d/{SPOUSE_FILE_ID}/view?usp=sharing"

# A second photo for the same slot, to exercise a changed sheet link.
NEW_FILE_ID = "1NewPhotoFileIdAbCdEfGhIjKl"
NEW_DRIVE_LINK = f"https://drive.google.com/open?id={NEW_FILE_ID}"
NEW_BYTES = PNG_BYTES + b"\x00"


class _FakeStorage(StorageBackend):
    """In-memory storage so the tests never touch a real bucket."""

    available = True
    serves_public_urls = True
    public_url = "https://photos.example.test"

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.uploads: list[str] = []

    def upload_bytes(self, object_key, data, mime_type):
        self.objects[object_key] = (data, mime_type)
        self.uploads.append(object_key)
        return f"{self.public_url}/{object_key}"

    def read_bytes(self, object_key):
        return self.objects[object_key][0]

    def object_exists(self, object_key):
        return object_key in self.objects

    def find_object_key(self, prefix):
        for key in self.objects:
            if key.startswith(prefix):
                return key
        return None

    def delete_object(self, object_key):
        self.objects.pop(object_key, None)


class _FakeDownloader:
    """Returns canned bytes, or fails the way Drive does for a private file."""

    def __init__(self, photos: dict[str, DownloadedPhoto] | None = None) -> None:
        self.photos = photos or {}
        self.failures: dict[str, str] = {}
        self.requested: list[str] = []

    def download(self, link: PhotoLink, role: str) -> DownloadedPhoto:
        file_id = link.file_id or ""
        self.requested.append(file_id)
        if file_id in self.failures:
            raise DrivePhotoError(self.failures[file_id])
        return self.photos.get(
            file_id,
            DownloadedPhoto(PNG_BYTES, "image/png", f"{role}.png"),
        )


@pytest.fixture
def storage() -> _FakeStorage:
    return _FakeStorage()


@pytest.fixture
def downloader() -> _FakeDownloader:
    return _FakeDownloader()


@asynccontextmanager
async def _session():
    """A session whose changes the test owns, rolled back at the end.

    Opened inside the test's own event loop: the async engine pools
    connections, and a connection opened in a different loop cannot be reused.
    Members are created through this same session so the migration reads them
    without a second connection racing the transaction.
    """
    from app.database import async_session_factory

    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


async def _make_member(session, **kwargs) -> uuid.UUID:
    member = Member(
        first_name=kwargs.pop("first_name", "Photo"),
        last_name=kwargs.pop("last_name", "Tester"),
        membership_status="APPROVED",
        **kwargs,
    )
    session.add(member)
    await session.flush()
    return member.id


async def _load(session, member_id: uuid.UUID) -> Member:
    """Re-read a member from the database, ignoring the identity map.

    The migration writes with ``session.flush``, so the attributes in the
    identity map are already current. This exists to assert the values the
    application would actually read, not an in-memory expectation.
    """
    return await session.get(Member, member_id)


async def _run(db, storage, downloader, **kwargs):
    migration = DrivePhotoMigration(db, storage=storage, downloader=downloader)
    return await migration.run(**kwargs)


# The module-level engine's pool is bound to one event loop, and other test
# modules drive the same engine from a different loop. Without disposing the
# pool between tests, asyncpg raises "attached to a different loop".
# test_schema_guard.py handles this the same way.
pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.filterwarnings("ignore::pytest.PytestWarning"),
]


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _fresh_pool():
    await engine.dispose()
    yield
    await engine.dispose()


# ----------------------------------------------------------------------
# link parsing
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "url,expected_id",
    [
        (
            "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUvWxYz234567",
            "1AbCdEfGhIjKlMnOpQrStUvWxYz234567",
        ),
        (
            "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/view?usp=sharing",
            "1AbCdEfGhIjKlMnOpQrStUvWxYz",
        ),
        (
            "https://drive.google.com/uc?export=view&id=1AbCdEfGhIjKlMnOpQrStUvWxYz234567",
            "1AbCdEfGhIjKlMnOpQrStUvWxYz234567",
        ),
        (
            "https://drive.google.com/uc?export=download&id=1AbCdEfGhIjKlMnOpQrStUvWxYz234567",
            "1AbCdEfGhIjKlMnOpQrStUvWxYz234567",
        ),
        (
            "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUvWxYz234567&usp=sharing",
            "1AbCdEfGhIjKlMnOpQrStUvWxYz234567",
        ),
    ],
)
def test_every_drive_link_shape_yields_a_file_id(url, expected_id):
    link = parse_photo_link(url)
    assert link.kind is PhotoLinkKind.FILE_ID
    assert link.file_id == expected_id
    assert is_drive_photo_url(url)


def test_googleusercontent_link_is_direct_and_left_alone():
    url = "https://lh3.googleusercontent.com/u/0/yd2ABCdEfGhIjKlMnOp=photo.jpg"
    assert parse_photo_link(url).kind is PhotoLinkKind.DIRECT_IMAGE
    assert is_direct_image_url(url)
    assert not is_drive_photo_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        None,
        "not-a-url",
        "/dev-media/image/a/b.jpg",
        "https://jssgapi.vercel.app/api/v1/media/abc/content",
        "https://pub-abc123.r2.dev/image/a/b.jpg",
    ],
)
def test_non_drive_links_are_unsupported(url):
    assert parse_photo_link(url).kind is PhotoLinkKind.UNSUPPORTED


# ----------------------------------------------------------------------
# migration behaviour
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_one_member_with_a_drive_link_is_migrated():
    async with _session() as db_session:
        """The reported case: one member, one Drive link, real bytes in R2."""
        storage, downloader = _FakeStorage(), _FakeDownloader()
        member_id = await _make_member(
            db_session, profile_photo_url=DRIVE_LINK, first_name="one"
        )
        summary = await _run(db_session, storage, downloader, dry_run=False)
        assert summary.migrated == 1
        assert summary.failed == 0
        assert downloader.requested == [JPEG_FILE_ID]

        member = await _load(db_session, member_id)
        expected_key = f"members/{member_id}/photos/member.png"
        assert storage.objects[expected_key][0] == PNG_BYTES
        assert storage.objects[expected_key][1] == "image/png"

        # The URL the app renders now points at R2, not at Drive.
        assert member.profile_photo_url == (
            f"https://photos.example.test/{expected_key}"
        )
        assert member.profile_data["member_photo_link"] == member.profile_photo_url
        # The original link is preserved so the photo stays re-syncable.
        assert member.profile_photo_drive_url == DRIVE_LINK
        assert member.profile_photo_r2_object_key == expected_key

        # And the media row exists so the content route can serve the bytes.
        media_key = await db_session.scalar(
            select(Media.r2_object_key).where(Media.owner_id == member_id)
        )
        assert media_key == expected_key


@pytest.mark.asyncio
async def test_rerunning_migrates_nothing_twice():
    async with _session() as db_session:
        storage, downloader = _FakeStorage(), _FakeDownloader()
        await _make_member(db_session, profile_photo_url=DRIVE_LINK, first_name="twice")
        first = await _run(db_session, storage, downloader, dry_run=False)
        assert first.migrated == 1

        second = await _run(db_session, storage, downloader, dry_run=False)
        assert second.migrated == 0
        assert second.already_migrated == 1
        # The whole point: no second upload, no second download.
        assert len(storage.uploads) == 1
        assert downloader.requested == [JPEG_FILE_ID]


@pytest.mark.asyncio
async def test_dry_run_changes_nothing():
    async with _session() as db_session:
        storage, downloader = _FakeStorage(), _FakeDownloader()
        member_id = await _make_member(
            db_session, profile_photo_url=DRIVE_LINK, first_name="dry"
        )
        summary = await _run(db_session, storage, downloader, dry_run=True)
        assert summary.migrated == 1
        assert summary.dry_run is True
        # Nothing written: no upload, no download, member untouched.
        assert storage.objects == {}
        assert downloader.requested == []

        member = await _load(db_session, member_id)
        assert member.profile_photo_url == DRIVE_LINK
        assert member.profile_photo_r2_object_key is None


@pytest.mark.asyncio
async def test_a_private_drive_file_fails_without_stopping_the_run():
    async with _session() as db_session:
        """Failure isolation: one unreadable photo must not abort the batch."""
        storage = _FakeStorage()
        # DRIVE_LINK's file id is unreadable; SPOUSE_DRIVE_LINK's resolves.
        downloader = _FakeDownloader()
        downloader.failures[JPEG_FILE_ID] = (
            "drive file 1AbCdEfGhIjKlMnOpQrStUvWxYz234567 is not shared publicly "
            "and the service account cannot read it either"
        )
        broken_id = await _make_member(
            db_session, profile_photo_url=DRIVE_LINK, first_name="broken"
        )
        healthy_id = await _make_member(
            db_session, profile_photo_url=SPOUSE_DRIVE_LINK, first_name="healthy"
        )
        summary = await _run(db_session, storage, downloader, dry_run=False)
        assert summary.migrated == 1
        assert summary.failed == 1

        failures = summary.to_dict()["failures"]
        assert len(failures) == 1
        assert failures[0]["member_id"] == str(broken_id)
        assert failures[0]["role"] == "member"
        assert failures[0]["source_url"] == DRIVE_LINK
        assert "not shared publicly" in failures[0]["reason"]

        # The healthy member was still migrated.
        healthy = await _load(db_session, healthy_id)
        assert healthy.profile_photo_url.startswith("https://photos.example.test/")
        # The broken member is left exactly as it was.
        broken = await _load(db_session, broken_id)
        assert broken.profile_photo_url == DRIVE_LINK
        assert broken.profile_photo_r2_object_key is None


@pytest.mark.asyncio
async def test_both_slots_are_migrated_independently():
    async with _session() as db_session:
        storage, downloader = _FakeStorage(), _FakeDownloader()
        member_id = await _make_member(
            db_session,
            profile_photo_url=DRIVE_LINK,
            profile_data={"spouse_photo_link": SPOUSE_DRIVE_LINK},
            first_name="both",
        )
        summary = await _run(db_session, storage, downloader, dry_run=False)
        assert summary.migrated == 2
        member = await _load(db_session, member_id)
        assert member.profile_photo_r2_object_key == (
            f"members/{member_id}/photos/member.png"
        )
        assert member.spouse_photo_r2_object_key == (
            f"members/{member_id}/photos/spouse.png"
        )
        assert member.spouse_photo_drive_url == SPOUSE_DRIVE_LINK
        # The spouse link is rewritten where the app reads it.
        assert member.profile_data["spouse_photo_link"] == (
            f"https://photos.example.test/members/{member_id}/photos/spouse.png"
        )


@pytest.mark.asyncio
async def test_direct_image_url_is_skipped_and_left_intact():
    async with _session() as db_session:
        storage, downloader = _FakeStorage(), _FakeDownloader()
        url = "https://lh3.googleusercontent.com/u/0/abc123=photo.jpg"
        member_id = await _make_member(
            db_session, profile_photo_url=url, first_name="direct"
        )
        summary = await _run(db_session, storage, downloader, dry_run=False)
        assert summary.migrated == 0
        assert summary.skipped == 1
        assert storage.objects == {}
        # The link is left exactly as it was: it already serves image bytes.
        assert (await _load(db_session, member_id)).profile_photo_url == url


@pytest.mark.asyncio
async def test_member_without_any_photo_is_not_reported():
    async with _session() as db_session:
        storage, downloader = _FakeStorage(), _FakeDownloader()
        await _make_member(db_session, profile_photo_url=None, first_name="bare")
        summary = await _run(db_session, storage, downloader, dry_run=False)
        assert summary.scanned == 0
        assert summary.migrated == 0
        assert summary.outcomes == []


@pytest.mark.asyncio
async def test_object_in_bucket_without_a_db_record_is_not_duplicated():
    async with _session() as db_session:
        """Self-healing: a lost DB write must not cause a second copy."""
        storage, downloader = _FakeStorage(), _FakeDownloader()
        member_id = await _make_member(
            db_session, profile_photo_url=DRIVE_LINK, first_name="heal"
        )
        # Bytes already in the bucket, no record on the member.
        key = f"members/{member_id}/photos/member.png"
        storage.objects[key] = (PNG_BYTES, "image/png")

        summary = await _run(db_session, storage, downloader, dry_run=False)
        assert summary.migrated == 0
        assert summary.already_migrated == 1
        assert storage.uploads == []
        assert downloader.requested == []
        # The record is repaired from the key that was found.
        assert (await _load(db_session, member_id)).profile_photo_r2_object_key == key


@pytest.mark.asyncio
async def test_recorded_key_whose_object_vanished_is_re_migrated():
    async with _session() as db_session:
        storage, downloader = _FakeStorage(), _FakeDownloader()
        await _make_member(
            db_session, profile_photo_url=DRIVE_LINK, first_name="vanish"
        )
        summary = await _run(db_session, storage, downloader, dry_run=False)
        assert summary.migrated == 1

        # Simulate the bucket being emptied underneath the database.
        storage.objects.clear()
        summary = await _run(db_session, storage, downloader, dry_run=False)
        assert summary.migrated == 1
        assert len(storage.objects) == 1


@pytest.mark.asyncio
async def test_changed_sheet_link_replaces_the_stored_photo():
    """A new photo link in the sheet must replace the old stored bytes."""
    async with _session() as db_session:
        storage = _FakeStorage()
        downloader = _FakeDownloader(
            {NEW_FILE_ID: DownloadedPhoto(NEW_BYTES, "image/png", "member.png")}
        )
        member_id = await _make_member(
            db_session, profile_photo_url=DRIVE_LINK, first_name="replace"
        )
        first = await _run(db_session, storage, downloader, dry_run=False)
        assert first.migrated == 1
        key = f"members/{member_id}/photos/member.png"
        assert storage.objects[key][0] == PNG_BYTES

        # What the sheet sync writes when the admin replaces the photo link.
        member = await _load(db_session, member_id)
        member.profile_photo_url = NEW_DRIVE_LINK
        member.profile_data = {"member_photo_link": NEW_DRIVE_LINK}
        await db_session.flush()

        second = await _run(db_session, storage, downloader, dry_run=False)
        assert second.replaced == 1, second.to_dict()
        assert second.migrated == 0
        # Same extension -> same key, overwritten with the new bytes.
        assert storage.objects[key][0] == NEW_BYTES
        member = await _load(db_session, member_id)
        assert member.profile_photo_url == f"https://photos.example.test/{key}"
        # The preserved source now points at the new file, so a re-run is a no-op.
        assert member.profile_photo_drive_url == NEW_DRIVE_LINK
        third = await _run(db_session, storage, downloader, dry_run=False)
        assert third.already_migrated == 1
        assert third.replaced == 0


@pytest.mark.asyncio
async def test_changed_sheet_link_dry_run_reports_replacement_only():
    async with _session() as db_session:
        storage = _FakeStorage()
        downloader = _FakeDownloader()
        member_id = await _make_member(
            db_session, profile_photo_url=DRIVE_LINK, first_name="dry-replace"
        )
        await _run(db_session, storage, downloader, dry_run=False)
        uploads_before = len(storage.uploads)

        member = await _load(db_session, member_id)
        member.profile_photo_url = NEW_DRIVE_LINK
        member.profile_data = {"member_photo_link": NEW_DRIVE_LINK}
        await db_session.flush()

        summary = await _run(db_session, storage, downloader, dry_run=True)
        assert summary.replaced == 1
        assert summary.dry_run is True
        assert len(storage.uploads) == uploads_before, "a dry run uploads nothing"


@pytest.mark.asyncio
async def test_replacement_with_a_new_extension_deletes_the_old_object():
    """A changed photo of a different type must not leave the old object behind."""
    async with _session() as db_session:
        storage = _FakeStorage()
        downloader = _FakeDownloader(
            {NEW_FILE_ID: DownloadedPhoto(NEW_BYTES, "image/jpeg", "photo.jpg")}
        )
        member_id = await _make_member(
            db_session, profile_photo_url=DRIVE_LINK, first_name="extension"
        )
        await _run(db_session, storage, downloader, dry_run=False)
        old_key = f"members/{member_id}/photos/member.png"
        assert old_key in storage.objects

        member = await _load(db_session, member_id)
        member.profile_photo_url = NEW_DRIVE_LINK
        member.profile_data = {"member_photo_link": NEW_DRIVE_LINK}
        await db_session.flush()

        summary = await _run(db_session, storage, downloader, dry_run=False)
        assert summary.replaced == 1
        new_key = f"members/{member_id}/photos/member.jpg"
        assert new_key in storage.objects
        assert old_key not in storage.objects, "the superseded object is deleted"

        keys = set(
            (
                await db_session.execute(
                    select(Media.r2_object_key).where(
                        Media.owner_id == member_id,
                        Media.is_deleted.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert keys == {new_key}, "the superseded media row is retired"


@pytest.mark.asyncio
async def test_spouse_photo_change_is_replaced_independently():
    async with _session() as db_session:
        storage = _FakeStorage()
        downloader = _FakeDownloader(
            {NEW_FILE_ID: DownloadedPhoto(NEW_BYTES, "image/png", "spouse.png")}
        )
        member_id = await _make_member(
            db_session,
            profile_photo_url=DRIVE_LINK,
            profile_data={"spouse_photo_link": SPOUSE_DRIVE_LINK},
            first_name="spouse-replace",
        )
        first = await _run(db_session, storage, downloader, dry_run=False)
        assert first.migrated == 2

        member = await _load(db_session, member_id)
        profile = dict(member.profile_data or {})
        profile["spouse_photo_link"] = NEW_DRIVE_LINK
        member.profile_data = profile
        await db_session.flush()

        second = await _run(db_session, storage, downloader, dry_run=False)
        assert second.replaced == 1
        # The member photo is unchanged and stays as it was.
        assert second.already_migrated == 1
        member = await _load(db_session, member_id)
        assert member.spouse_photo_drive_url == NEW_DRIVE_LINK


@pytest.mark.asyncio
async def test_storage_outage_is_reported_not_silently_ignored():
    async with _session() as db_session:

        class _Down(_FakeStorage):
            available = False

            @property
            def unavailable_message(self) -> str:
                return "Media storage is not available"

        downloader = _FakeDownloader()
        await _make_member(db_session, profile_photo_url=DRIVE_LINK, first_name="down")
        from app.core.exceptions import ServiceUnavailableException

        with pytest.raises(ServiceUnavailableException):
            await _run(db_session, _Down(), downloader, dry_run=False)
        # A dry run still works, so an operator can see the plan during an outage.
        summary = await _run(db_session, _Down(), downloader, dry_run=True)
        assert summary.migrated == 1


# ----------------------------------------------------------------------
# the 500 that motivated this
# ----------------------------------------------------------------------
def _r2_backend_with(client) -> CloudflareR2Client:
    """A client wired to a stub, bypassing the real credential check.

    The route's behaviour when a request fails does not depend on the
    configured endpoint, and the test must not depend on a real bucket.
    """
    backend = CloudflareR2Client.__new__(CloudflareR2Client)
    backend.endpoint = "https://account.r2.cloudflarestorage.com"
    backend.access_key = "test-access-key"
    backend.secret_key = "test-secret-key"
    backend.bucket = "jssg-media"
    backend.public_url = ""
    backend.missing = []
    backend.configuration_error = ""
    backend.available = True
    backend.serves_public_urls = False
    backend._boto3_client = client
    return backend


def test_missing_storage_object_is_not_an_unhandled_500():
    """A media row whose object is gone must raise a typed error.

    The raw botocore ``NoSuchKey`` used to escape the media route as a 500.
    """
    from app.core.exceptions import ServiceUnavailableException

    class _MissingClient:
        def get_object(self, **kwargs):
            raise _client_error("NoSuchKey", 404)

        def head_object(self, **kwargs):
            raise _client_error("NoSuchKey", 404)

    backend = _r2_backend_with(_MissingClient())

    with pytest.raises(ServiceUnavailableException) as excinfo:
        backend.read_bytes("image/someone/missing.jpg")
    assert excinfo.value.error_code == "MEDIA_NOT_FOUND"
    assert excinfo.value.status_code == 503

    # And it reads as "missing" rather than as a storage outage.
    assert backend.object_exists("image/someone/missing.jpg") is False


def test_signature_mismatch_is_reported_as_a_configuration_error():
    """Wrong endpoint or key pair must name itself, not surface as a 500."""
    from app.core.exceptions import ServiceUnavailableException

    class _BadSignatureClient:
        def get_object(self, **kwargs):
            raise _client_error("SignatureDoesNotMatch", 403)

    backend = _r2_backend_with(_BadSignatureClient())

    with pytest.raises(ServiceUnavailableException) as excinfo:
        backend.read_bytes("image/a/b.jpg")
    assert excinfo.value.error_code == "MEDIA_STORAGE_ERROR"
    assert "SignatureDoesNotMatch" in excinfo.value.detail
    # An unknown outcome is a failure, not a silent "not there".
    with pytest.raises(ServiceUnavailableException):
        backend.object_exists("image/a/b.jpg")


def _client_error(code: str, status: int) -> Exception:
    class _ClientError(Exception):
        def __init__(self) -> None:
            super().__init__(code)
            self.response = {
                "Error": {"Code": code, "Message": code},
                "ResponseMetadata": {"HTTPStatusCode": status},
            }

    return _ClientError()


async def test_local_disk_backend_reports_whether_an_object_exists(tmp_path):
    backend = LocalDiskStorage(tmp_path)
    assert backend.object_exists("image/a/b.jpg") is False
    backend.upload_bytes("image/a/b.jpg", PNG_BYTES, "image/png")
    assert backend.object_exists("image/a/b.jpg") is True
    assert backend.read_bytes("image/a/b.jpg") == PNG_BYTES


# ----------------------------------------------------------------------
# Drive API disabled vs. no access
# ----------------------------------------------------------------------
def test_api_disabled_is_detected_from_error_details():
    from app.services.google_drive_photos import _is_api_disabled

    class _HttpError(Exception):
        def __init__(self, details):
            super().__init__("boom")
            self.error_details = details

    assert _is_api_disabled(
        _HttpError([{"reason": "accessNotConfigured", "domain": "usageLimits"}])
    )
    assert _is_api_disabled(_HttpError([{"reason": "serviceDisabled"}]))
    # A plain "file not found" is a sharing problem, not a disabled API.
    assert not _is_api_disabled(_HttpError([{"reason": "notFound"}]))


def test_api_disabled_is_detected_from_the_message_when_details_are_absent():
    from app.services.google_drive_photos import _is_api_disabled

    assert _is_api_disabled(
        Exception(
            "Google Drive API has not been used in project 123 before or it is "
            "disabled."
        )
    )
    assert not _is_api_disabled(Exception("file not found"))


def test_downloader_reports_a_disabled_drive_api_rather_than_falling_back(
    monkeypatch,
):
    """A disabled API is an operator fix, not a silent "no access" fallback."""
    from app.services.google_drive_photos import DrivePhotoDownloader

    downloader = DrivePhotoDownloader(settings=object(), timeout=1)

    class _HttpError(Exception):
        def __init__(self) -> None:
            super().__init__("Google Drive API has not been used in project 1")
            self.error_details = [{"reason": "accessNotConfigured"}]

    def _boom():
        raise _HttpError()

    monkeypatch.setattr(downloader, "_drive_service", _boom)

    link = PhotoLink(PhotoLinkKind.FILE_ID, url=DRIVE_LINK, file_id=JPEG_FILE_ID)
    with pytest.raises(DrivePhotoError) as excinfo:
        downloader.download(link, "member")
    assert "Drive API is not enabled" in excinfo.value.reason


def test_downloader_falls_back_to_the_public_path_on_a_sharing_error(monkeypatch):
    """A file the service account cannot see must still be tried anonymously."""
    from app.services.google_drive_photos import DrivePhotoDownloader

    downloader = DrivePhotoDownloader(settings=object(), timeout=1)

    class _NotFound(Exception):
        def __init__(self) -> None:
            super().__init__("File not found: abc")
            self.error_details = [{"reason": "notFound"}]

    def _boom():
        raise _NotFound()

    monkeypatch.setattr(downloader, "_drive_service", _boom)

    called: dict[str, object] = {}

    def _fake_public(file_id, direct_url, role):
        called["file_id"] = file_id
        return DownloadedPhoto(PNG_BYTES, "image/png", "member.png")

    monkeypatch.setattr(downloader, "_download_publicly", _fake_public)

    link = PhotoLink(PhotoLinkKind.FILE_ID, url=DRIVE_LINK, file_id=JPEG_FILE_ID)
    photo = downloader.download(link, "member")
    assert called["file_id"] == JPEG_FILE_ID
    assert photo.content == PNG_BYTES
