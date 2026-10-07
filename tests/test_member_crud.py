"""Member CRUD, member/spouse photo uploads, and the sheet-backed group list.

The photo tests run against an in-memory storage backend, so they prove what
the API writes to Postgres and what it asks storage to keep without ever
touching a real bucket.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models.group import SocialGroup
from app.models.media import Media, MediaObject
from app.models.member import Member
from app.models.user import Role, User
from app.services.google_sheets_service import SheetTable
from app.services.media_storage import StorageBackend

PASSWORD = "StrongPass123!"

# Real image bytes: uploads are validated against the actual content, so a test
# cannot use arbitrary bytes and still be accepted as a photo.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90\x77\x53\xde"
    b"\x00\x00\x00\x0cIDATx\xda\x63\xe0\x16\x53\x04\x00\x00\x72\x00C\x0e\x34\xba&"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
GIF_BYTES = (
    b"GIF89a"
    b"\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"\x21\xf9\x04\x01\x00\x00\x00\x00"
    b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00"
    b"\x02\x02\x44\x01\x00;"
)

CDN = "https://cdn.test/"

_created_members: list[str] = []
_created_groups: list[str] = []


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


class _RecordingStorage(StorageBackend):
    """In-memory storage that remembers every object it was asked to keep."""

    available = True
    serves_public_urls = True

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload_bytes(self, object_key, data, mime_type):
        self.objects[object_key] = data
        return f"{CDN}{object_key}"

    def read_bytes(self, object_key):
        return self.objects[object_key]

    def object_exists(self, object_key):
        return object_key in self.objects

    def delete_object(self, object_key):
        self.objects.pop(object_key, None)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def _mock_firebase():
    from unittest import mock

    def _fake_verify_id_token(id_token: str) -> dict:
        return {"uid": f"membercrud-{id_token}", "phone_number": id_token}

    with mock.patch(
        "app.services.auth_service.firebase_verify_id_token", _fake_verify_id_token
    ):
        yield


@pytest.fixture(scope="module")
def auth(client: TestClient, _mock_firebase) -> dict:
    """A logged-in SUPER_ADMIN."""
    import time

    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    phone = f"+916{str(time.time_ns())[-9:]}"
    try:
        with Session(engine) as session:
            role = session.execute(
                select(Role).where(Role.name == "SUPER_ADMIN")
            ).scalar_one()
            session.add(
                User(
                    phone_number=phone,
                    password_hash=hash_password(PASSWORD),
                    full_name="Member CRUD Admin",
                    is_email_verified=True,
                    is_phone_verified=True,
                    role_id=role.id,
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


@pytest.fixture(scope="module")
def storage():
    """Patch the storage backend the photo service resolves at request time."""
    fake = _RecordingStorage()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "app.services.member_photo_service.get_storage", lambda: fake
        )
        yield fake


@pytest.fixture(scope="module", autouse=True)
def _engine():
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    yield engine
    with Session(engine) as session:
        if _created_members:
            object_keys = select(Media.r2_object_key).where(
                Media.owner_id.in_(_created_members)
            )
            session.execute(
                delete(MediaObject).where(MediaObject.object_key.in_(object_keys))
            )
            session.execute(delete(Media).where(Media.owner_id.in_(_created_members)))
            # family, professional and payment rows follow the member; Postgres
            # handles them through the FKs on the members table.
            session.execute(delete(Member).where(Member.id.in_(_created_members)))
        if _created_groups:
            session.execute(
                delete(SocialGroup).where(SocialGroup.id.in_(_created_groups))
            )
        session.commit()
    engine.dispose()


def _member_row(engine, member_id: str) -> Member:
    with Session(engine) as session:
        member = session.execute(
            select(Member).where(Member.id == member_id)
        ).scalar_one()
        session.expunge(member)
        return member


def _live_media_keys(engine, member_id: str) -> list[str]:
    with Session(engine) as session:
        rows = session.execute(
            select(Media.r2_object_key).where(
                Media.owner_id == member_id, Media.is_deleted.is_(False)
            )
        ).scalars()
        return list(rows)


def _create_member(client: TestClient, auth: dict, **overrides) -> dict:
    payload = {
        "full_name": "Ramesh Kumar Verma",
        "mobile_number": "9811111111",
        "family_member_count": 4,
        "date_of_birth": "12/05/1990",
        "gender": "Male",
        "address": "12 MG Road",
        "city": "Indore",
        "area": "Vijay Nagar",
        "company_business_name": "Verma Traders",
        "interest_field_of_social_activities": "Reading, Cricket",
        "business_address": "42 Market Road, Indore",
        "member_photo_url": "https://drive.example/member.jpg",
        "unmarried_son1_details": "Rohit Verma",
        "unmarried_son2_details": "Karan Verma",
        "unmarried_daughter2_details": "Anjali Verma",
    }
    payload.update(overrides)
    response = client.post("/api/v1/members", json=payload, headers=auth)
    assert response.status_code in (200, 201), response.text
    member = response.json()["data"]
    _created_members.append(member["id"])
    return member


def _create_group(client: TestClient, auth: dict, name: str) -> dict:
    response = client.post("/api/v1/groups", json={"name": name}, headers=auth)
    assert response.status_code in (200, 201), response.text
    group = response.json()["data"]
    _created_groups.append(group["id"])
    return group


# --- create / read / update ------------------------------------------------


def test_create_member_accepts_the_spec_field_names(client, auth):
    member = _create_member(client, auth)
    assert member["first_name"] == "Ramesh"
    assert member["last_name"] == "Verma"
    assert member["phone_number"] == "9811111111"
    # Counts and dates arrive as numbers/text and are stored as text.
    assert member["family_member_count"] == "4"
    assert member["date_of_birth"] == "1990-05-12"
    assert member["company_name"] == "Verma Traders"
    assert member["interest_fields"] == "Reading, Cricket"
    assert member["business_address"] == "42 Market Road, Indore"
    assert member["member_photo_link"] == "https://drive.example/member.jpg"
    assert member["profile_photo_url"] == "https://drive.example/member.jpg"
    assert member["unmarried_son1_details"] == "Rohit Verma"
    assert member["unmarried_son2_details"] == "Karan Verma"
    assert member["unmarried_daughter2_details"] == "Anjali Verma"
    # The first son keeps both spellings of the same slot in step.
    assert member["son_name"] == "Rohit Verma"
    # Response keys stay canonical, whatever the request was called.
    assert "mobile_number" not in member
    assert "company_business_name" not in member


def test_create_member_rejects_a_payload_without_a_name(client, auth):
    response = client.post(
        "/api/v1/members",
        json={"phone_number": "9822222222", "family_member_count": 2},
        headers=auth,
    )
    assert response.status_code == 422, response.text
    assert "detail" in response.json()


def test_get_member_returns_the_profile_fields_too(client, auth):
    member = _create_member(client, auth)
    response = client.get(f"/api/v1/members/{member['id']}", headers=auth)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["id"] == member["id"]
    assert data["phone_number"] == "9811111111"
    assert data["city"] == "Indore"
    assert data["family_member_count"] == "4"
    assert data["unmarried_son2_details"] == "Karan Verma"


def test_patch_updates_only_the_fields_it_is_sent(client, auth):
    member = _create_member(client, auth)
    response = client.patch(
        f"/api/v1/members/{member['id']}",
        json={"first_name": "Rameshwar", "city": "Bhopal"},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["first_name"] == "Rameshwar"
    assert data["city"] == "Bhopal"
    # Everything the patch did not mention is still there.
    assert data["last_name"] == "Verma"
    assert data["phone_number"] == "9811111111"
    assert data["company_name"] == "Verma Traders"
    assert data["business_address"] == "42 Market Road, Indore"
    assert data["family_member_count"] == "4"


def test_patch_accepts_an_integer_family_member_count(client, auth):
    member = _create_member(client, auth)
    response = client.patch(
        f"/api/v1/members/{member['id']}",
        json={"family_member_count": 7},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["family_member_count"] == "7"


def test_patch_clears_only_an_explicitly_nulled_profile_field(client, auth):
    member = _create_member(client, auth)
    response = client.patch(
        f"/api/v1/members/{member['id']}",
        json={"business_address": None, "area": None, "first_name": "Kept"},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["business_address"] is None
    assert data["area"] is None
    assert data["address"] == "12 MG Road"
    assert data["first_name"] == "Kept"


def test_patch_ignores_a_nulled_member_column(client, auth):
    """A column the panel sends as null must not be erased by a PATCH."""
    member = _create_member(client, auth)
    response = client.patch(
        f"/api/v1/members/{member['id']}",
        json={"profile_photo_url": None, "first_name": "Ramesh"},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    assert (
        response.json()["data"]["profile_photo_url"]
        == "https://drive.example/member.jpg"
    )


def test_get_and_patch_a_missing_member_is_404(client, auth):
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/v1/members/{missing}", headers=auth).status_code == 404
    assert (
        client.patch(
            f"/api/v1/members/{missing}", json={"first_name": "x"}, headers=auth
        ).status_code
        == 404
    )


# --- photos ----------------------------------------------------------------


def test_member_photo_upload_replace_and_delete(client, auth, storage, _engine):
    member = _create_member(client, auth)
    url_path = f"/api/v1/members/{member['id']}/photo/member"

    # Upload.
    response = client.post(
        url_path,
        files={"photo": ("member.png", PNG_BYTES, "image/png")},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    photo_url = body["photo_url"]
    assert photo_url == body["data"]["photo_url"]
    assert photo_url.startswith(CDN)
    assert f"/members/{member['id']}/member/" in photo_url
    first_key = photo_url.removeprefix(CDN)
    assert first_key in storage.objects

    # Postgres holds the URL and the key, never the bytes.
    row = _member_row(_engine, member["id"])
    assert row.profile_photo_url == photo_url
    assert row.profile_photo_r2_url == photo_url
    assert row.profile_photo_r2_object_key == first_key
    assert row.profile_data["member_photo_link"] == photo_url
    assert _live_media_keys(_engine, member["id"]) == [first_key]

    # The read API agrees with what was written.
    detail = client.get(f"/api/v1/members/{member['id']}", headers=auth).json()["data"]
    assert detail["profile_photo_url"] == photo_url
    assert detail["member_photo_link"] == photo_url

    # Replace: the new object is stored first, then the old one is removed.
    response = client.post(
        url_path,
        files={"photo": ("member2.gif", GIF_BYTES, "image/gif")},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    second_url = response.json()["photo_url"]
    second_key = second_url.removeprefix(CDN)
    assert second_key != first_key
    assert first_key not in storage.objects
    assert second_key in storage.objects
    assert _live_media_keys(_engine, member["id"]) == [second_key]

    # A file that is not an image is refused, and the photo survives it.
    response = client.post(
        url_path,
        files={"photo": ("evil.jpg", b"this is not an image", "image/jpeg")},
        headers=auth,
    )
    assert response.status_code == 400, response.text
    assert response.json()["success"] is False
    assert second_key in storage.objects
    row = _member_row(_engine, member["id"])
    assert row.profile_photo_r2_object_key == second_key
    assert row.profile_photo_url == second_url

    # Remove.
    response = client.delete(url_path, headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["photo_url"] is None
    assert second_key not in storage.objects
    row = _member_row(_engine, member["id"])
    assert row.profile_photo_r2_object_key is None
    assert row.profile_photo_r2_url is None
    assert row.profile_photo_url is None
    assert row.profile_data["member_photo_link"] is None
    assert _live_media_keys(_engine, member["id"]) == []


def test_spouse_photo_is_kept_separate(client, auth, storage, _engine):
    member = _create_member(client, auth)
    # ``file`` is the other field name the API already accepts uploads as.
    response = client.post(
        f"/api/v1/members/{member['id']}/photo/spouse",
        files={"file": ("spouse.png", PNG_BYTES, "image/png")},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    photo_url = response.json()["photo_url"]
    object_key = photo_url.removeprefix(CDN)

    row = _member_row(_engine, member["id"])
    assert row.spouse_photo_r2_object_key == object_key
    assert row.spouse_photo_r2_url == photo_url
    assert row.profile_data["spouse_photo_link"] == photo_url
    # The member's own slot was not touched.
    assert row.profile_photo_r2_object_key is None
    assert row.profile_photo_url == "https://drive.example/member.jpg"


def test_photo_upload_validates_the_slot_and_the_session(client, auth, storage):
    member = _create_member(client, auth)
    url_path = f"/api/v1/members/{member['id']}/photo/avatar"
    assert (
        client.post(
            url_path,
            files={"photo": ("a.png", PNG_BYTES, "image/png")},
            headers=auth,
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/v1/members/{member['id']}/photo/member",
            files={"photo": ("a.png", PNG_BYTES, "image/png")},
        ).status_code
        == 401
    )
    missing = "00000000-0000-0000-0000-000000000000"
    assert (
        client.post(
            f"/api/v1/members/{missing}/photo/member",
            files={"photo": ("a.png", PNG_BYTES, "image/png")},
            headers=auth,
        ).status_code
        == 404
    )
    # No file at all.
    assert client.post(
        f"/api/v1/members/{member['id']}/photo/member", headers=auth
    ).status_code in (400, 422)


def test_delete_member_hides_it_and_removes_its_photos(client, auth, storage, _engine):
    member = _create_member(client, auth)
    response = client.post(
        f"/api/v1/members/{member['id']}/photo/member",
        files={"photo": ("member.png", PNG_BYTES, "image/png")},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    object_key = response.json()["photo_url"].removeprefix(CDN)
    assert object_key in storage.objects

    response = client.delete(f"/api/v1/members/{member['id']}", headers=auth)
    assert response.status_code == 200, response.text
    # The stored photo goes with the member.
    assert object_key not in storage.objects
    row = _member_row(_engine, member["id"])
    assert row.is_deleted is True
    assert row.profile_photo_r2_object_key is None
    assert _live_media_keys(_engine, member["id"]) == []

    # ...and the member is gone from reads.
    assert (
        client.get(f"/api/v1/members/{member['id']}", headers=auth).status_code == 404
    )
    listing = client.get(
        "/api/v1/members", params={"page_size": 100}, headers=auth
    ).json()
    assert member["id"] not in {item["id"] for item in listing["data"]}


# --- group filter and rename -----------------------------------------------


def test_members_can_be_filtered_by_group(client, auth):
    """The panel's Group dropdown passes group_id; it must actually narrow the
    list (it previously had no effect at all for a SUPER_ADMIN caller)."""
    stamp = uuid4().hex[:8]
    group = _create_group(client, auth, f"Filter Group {stamp}")
    other_group = _create_group(client, auth, f"Other Group {stamp}")
    grouped = _create_member(
        client,
        auth,
        full_name="Grouped Member",
        mobile_number="9810000001",
        group_id=group["id"],
    )
    elsewhere = _create_member(
        client,
        auth,
        full_name="Elsewhere Member",
        mobile_number="9810000002",
        group_id=other_group["id"],
    )
    loose = _create_member(
        client, auth, full_name="Loose Member", mobile_number="9810000003"
    )

    listing = client.get(
        "/api/v1/members",
        params={"group_id": group["id"], "page_size": 100},
        headers=auth,
    )
    assert listing.status_code == 200, listing.text
    payload = listing.json()
    ids = {item["id"] for item in payload["data"]}
    assert grouped["id"] in ids
    assert elsewhere["id"] not in ids
    assert loose["id"] not in ids
    assert payload["pagination"]["total"] == len(ids)

    # A search must honour the same filter instead of searching every group.
    searched = client.get(
        "/api/v1/members",
        params={"group_id": group["id"], "query": "Grouped"},
        headers=auth,
    )
    assert searched.status_code == 200, searched.text
    assert {item["id"] for item in searched.json()["data"]} == {grouped["id"]}


def test_patch_rename_updates_the_displayed_name(client, auth):
    """The directory shows profile_data.full_name while the panel edits the
    first/middle/last columns, so a rename has to be mirrored across."""
    member = _create_member(client, auth, full_name="Old Name Person")

    response = client.patch(
        f"/api/v1/members/{member['id']}",
        json={"first_name": "Renamed", "last_name": "Person"},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["full_name"] == "Renamed Person"
    assert response.json()["data"]["first_name"] == "Renamed"

    # ...unless the caller sends full_name itself, which then wins.
    response = client.patch(
        f"/api/v1/members/{member['id']}",
        json={"first_name": "Ignored", "full_name": "Explicit Name"},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["full_name"] == "Explicit Name"


# --- groups ----------------------------------------------------------------


def _sheet_table() -> SheetTable:
    return SheetTable(
        spreadsheet_id="test-sheet",
        sheet_name="Sheet1",
        header_row=1,
        data_start_row=2,
        columns=(
            ("A", "Social Group / Association Name"),
            ("B", "Group Designation"),
            ("C", "Full Name"),
        ),
        rows=(
            (2, ("ABJSSGF", "President", "Ramesh Verma")),
            (3, ("ABJSSGF", "President", "Suresh Patel")),
            (4, ("", "", "Anil Sharma")),
            (5, ("NA", "Member", "Kiran Joshi")),
            (6, ("Youth Circle", "Secretary", "Amit Gupta")),
        ),
    )


def test_groups_read_from_the_sheet(client, auth, monkeypatch):
    monkeypatch.setattr(
        "app.services.sheet_groups_service.GoogleSheetsService.read_table",
        lambda self, *args, **kwargs: _sheet_table(),
    )
    response = client.get("/api/v1/groups", params={"source": "sheets"}, headers=auth)
    assert response.status_code == 200, response.text
    items = response.json()["data"]
    # Duplicates collapse, placeholders ("NA", blank) are dropped, and the
    # values are sorted by name within each column.
    assert items == [
        {"id": "abjssgf", "name": "ABJSSGF", "field": "social_group_name"},
        {"id": "youth-circle", "name": "Youth Circle", "field": "social_group_name"},
        {"id": "member", "name": "Member", "field": "group_designation"},
        {"id": "president", "name": "President", "field": "group_designation"},
        {"id": "secretary", "name": "Secretary", "field": "group_designation"},
    ]


def test_groups_sheet_source_can_be_filtered(client, auth, monkeypatch):
    monkeypatch.setattr(
        "app.services.sheet_groups_service.GoogleSheetsService.read_table",
        lambda self, *args, **kwargs: _sheet_table(),
    )
    by_field = client.get(
        "/api/v1/groups",
        params={"source": "sheets", "field": "social_group_name"},
        headers=auth,
    )
    assert by_field.status_code == 200
    assert [item["name"] for item in by_field.json()["data"]] == [
        "ABJSSGF",
        "Youth Circle",
    ]

    by_query = client.get(
        "/api/v1/groups", params={"source": "sheets", "query": "youth"}, headers=auth
    )
    assert by_query.status_code == 200
    assert [item["name"] for item in by_query.json()["data"]] == ["Youth Circle"]

    # An unknown source is a client error, not a silent fallback.
    assert (
        client.get(
            "/api/v1/groups", params={"source": "excel"}, headers=auth
        ).status_code
        == 422
    )


def test_groups_default_source_is_still_the_database(client, auth):
    response = client.get("/api/v1/groups", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert "pagination" in body
    assert isinstance(body["data"], list)


@pytest.mark.skipif(
    not os.environ.get("LIVE_SHEET_TESTS"),
    reason="set LIVE_SHEET_TESTS=1 to read the real Google Sheet",
)
def test_groups_from_the_live_google_sheet(client, auth):
    response = client.get("/api/v1/groups", params={"source": "sheets"}, headers=auth)
    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert items, "the registration sheet should list at least one group"
    for item in items:
        assert item["field"] in ("social_group_name", "group_designation")
        assert item["name"].strip()
        assert item["id"]
