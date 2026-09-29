from __future__ import annotations

import time
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.security import create_access_token, hash_password
from app.main import app
from app.models.audit_log import AuditLog
from app.models.member import Member
from app.models.user import Role, User
from app.services.google_sheets_service import SheetTable
from app.services.registration_sync_service import (
    RegistrationSyncService,
    build_column_index,
    match_field,
    normalize_header,
    split_full_name,
)

PASSWORD = "StrongPass123!"
SHEET_ID = "1xT-3Lz9otqA3eL9A7dOoXJFyp4YEYu8lONjwWOVS6mI"
ENDPOINT = "/api/registration/sync-google-sheet"

HEADERS = [
    "Timestamp",
    "Full Name",
    "Date of Birth Member",
    "Date of Birth Spouse",
    "Date of Anniverssary ->",
    "Member Photo ->",
    "Spouse Name",
    "Spouse Mobile No.",
    "Number of Family Members",
    "Address / Location",
    "City",
    "Area",
    "Mobile Number",
    "Email ID",
    "Member Educational Qualification",
    "Member Occupation / Profession",
    "Company / Business Name",
    "Group Designation",
    "Social Group / Association Name",
    "Interest Field of Social Activities",
    "Profile Photo Member",
    "Definitely Not A Real Column",
]


def _row(*values: str) -> tuple[str, ...]:
    assert len(values) <= len(HEADERS)
    return tuple(values) + ("",) * (len(HEADERS) - len(values))


def _table(rows: list[tuple[str, ...]]) -> SheetTable:
    return SheetTable(
        spreadsheet_id=SHEET_ID,
        sheet_name="Form Responses 8",
        header_row=1,
        data_start_row=2,
        columns=tuple((chr(ord("A") + i), header) for i, header in enumerate(HEADERS)),
        rows=tuple((index + 2, row) for index, row in enumerate(rows)),
    )


_phone_counter = iter(range(1, 10_000))
_created_user_ids: list[object] = []


def _fresh_phone() -> str:
    # +91 followed by 9 digits: unique per call and unique per millisecond.
    serial = next(_phone_counter)
    return f"+916{int(time.time() * 1000) % 10**6:06d}{serial:03d}"


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


@pytest.fixture(scope="module", autouse=True)
def _cleanup_created_users():
    yield
    if not _created_user_ids:
        return
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    with Session(engine) as session:
        session.execute(delete(User).where(User.id.in_(_created_user_ids)))
        session.commit()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def admin_token() -> str:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    with Session(engine) as session:
        role = session.execute(select(Role).where(Role.name == "ADMIN")).scalar_one()
        user = User(
            phone_number=_fresh_phone(),
            password_hash=hash_password(PASSWORD),
            full_name="Sheets Sync Admin",
            is_email_verified=True,
            is_phone_verified=True,
            role_id=role.id,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        _created_user_ids.append(user.id)
        return create_access_token(subject=str(user.id))


def _counts() -> tuple[int, int, int]:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    with Session(engine) as session:
        users = session.execute(select(func.count()).select_from(User)).scalar_one()
        members = session.execute(select(func.count()).select_from(Member)).scalar_one()
        audits = session.execute(
            select(func.count()).select_from(AuditLog)
        ).scalar_one()
    return users, members, audits


def _call(client: TestClient, token: str, table: SheetTable, **body: object):
    payload = {"dry_run": True, "spreadsheet_id": SHEET_ID, **body}
    with (
        mock.patch(
            "app.services.registration_sync_service.GoogleSheetsService.read_table",
            return_value=table,
        ),
        mock.patch(
            "app.services.registration_sync_service.GoogleSheetsService.verify_access"
        ) as verify,
    ):
        verify.return_value = {
            "spreadsheet_id": SHEET_ID,
            "title": "JSSG Trial",
            "sheet_name": "Form Responses 8",
        }
        return client.post(
            ENDPOINT,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )


# --- pure helpers -------------------------------------------------------


def test_normalize_header_strips_symbols():
    assert normalize_header("Date of Anniverssary ->") == "date of anniverssary"
    assert normalize_header("Member Occupation / Profession") == (
        "member occupation profession"
    )


def test_build_column_index_maps_every_known_header():
    index = build_column_index()
    for header in HEADERS:
        key = match_field(index, header)
        if header == "Definitely Not A Real Column":
            assert key is None
        else:
            assert key is not None, header


def test_bilingual_photo_header_matches_the_base_alias():
    index = build_column_index()
    bilingual = (
        "Profile Photo Member\n\nकृपया फोटो का Google Drive link डालें / "
        "Please enter Google Drive photo link"
    )
    assert match_field(index, bilingual) == "member_photo_link"
    assert match_field(index, "Member Photo →") == "member_photo_link"


def test_split_full_name():
    assert split_full_name("Amit  Kumar  Sharma") == ("Amit", "Sharma")
    assert split_full_name("Amit") == ("Amit", "")


# --- endpoint -----------------------------------------------------------


def test_dry_run_writes_nothing(client: TestClient, admin_token: str):
    table = _table(
        [
            _row(
                "2026-04-01 10:00",
                "Asha Verma",
                "05/06/1990",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "9876500010",
            ),
        ]
        + [
            _row(
                "2026-04-01 10:01",
                "Ravi Kumar",
                "1988-12-01",
                "10/11/1992",
                "01/02/2010",
                "https://drive.google.com/file/d/abc/view",
                "Sunita",
                "9999999999",
                "2",
                "12 Park Street",
                "Delhi",
                "Saket",
                "9876543210",
                "ravi@example.com",
                "B.Tech",
                "Engineer",
                "Acme",
                "Coordinator",
                "JSSG Delhi",
                "Health",
                "https://drive.google.com/uc?id=xyz",
            )
        ]
    )
    before = _counts()
    response = _call(client, admin_token, table)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert data["dry_run"] is True
    assert data["inserted"] == 2
    assert data["linked_existing_user"] == 0
    assert data["skipped_duplicate"] == 0
    assert data["failed"] == 0
    assert data["unmapped_columns"] == ["Definitely Not A Real Column"]
    assert data["missing_columns"] == []
    assert {row["action"] for row in data["rows"]} == {"insert"}
    assert _counts() == before, "dry run must not write to PostgreSQL"


def test_dry_run_counts_duplicates_and_failures(client: TestClient, admin_token: str):
    phone = _fresh_phone()
    email_owner = f"sheet_owner_{int(time.time() * 1000)}@test.local"
    _create_user(_fresh_phone(), email_owner)
    table = _table(
        [
            _row("t", "Valid One", "", "", "", "", "", "", "", "", "", "", phone),
            _row("t", "Duplicate Row", "", "", "", "", "", "", "", "", "", "", phone),
            _row("t", "Bad Phone", "", "", "", "", "", "", "", "", "", "", "12"),
            _row("t", "", "", "", "", "", "", "", "", "", "", "", phone),
            _row("t", "Missing Phone"),
            _row(
                "t",
                "Email Clash",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "9876500000",
                email_owner,
            ),
        ]
    )
    before = _counts()
    response = _call(client, admin_token, table)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    actions = {row["row"]: row["action"] for row in data["rows"]}
    assert actions[2] == "insert"
    assert actions[3] == "skipped"
    assert actions[4] == "failed"
    assert actions[5] == "failed"
    assert actions[6] == "failed"
    assert actions[7] == "failed"
    assert data["inserted"] == 1
    assert data["skipped_duplicate"] == 1
    assert data["failed"] == 4
    assert _counts() == before


def _create_user(phone: str, email: str | None = None) -> int:
    """Create a plain user row (no member) so dry-run dedupe has a target."""
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    with Session(engine) as session:
        role = session.execute(select(Role).where(Role.name == "MEMBER")).scalar_one()
        user = User(
            phone_number=phone,
            email=email,
            password_hash=hash_password(PASSWORD),
            full_name="Existing Sheet Owner",
            is_phone_verified=True,
            role_id=role.id,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        _created_user_ids.append(user.id)
        return user.id


def test_dry_run_links_existing_user(client: TestClient, admin_token: str):
    phone = _fresh_phone()
    _create_user(phone)
    table = _table(
        [_row("t", "Existing User", "", "", "", "", "", "", "", "", "", "", phone)]
    )
    response = _call(client, admin_token, table)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["inserted"] == 0
    assert data["linked_existing_user"] == 1
    assert data["rows"][0]["action"] == "link"


def test_dry_run_flags_unreadable_dates(client: TestClient, admin_token: str):
    table = _table(
        [
            _row(
                "t",
                "Bad Dates",
                "not-a-date",
                "31/31/1990",
                "2010-02-30",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "9876500001",
            )
        ]
    )
    response = _call(client, admin_token, table)
    data = response.json()["data"]
    assert data["inserted"] == 1
    assert len(data["warnings"]) >= 2


def test_endpoint_requires_authentication(client: TestClient):
    response = client.post(ENDPOINT, json={"dry_run": True})
    assert response.status_code == 401


def test_write_mode_blocked_by_kill_switch(client: TestClient, admin_token: str):
    table = _table(
        [_row("t", "Blocked", "", "", "", "", "", "", "", "", "", "", "9876500002")]
    )
    response = _call(client, admin_token, table, dry_run=False)
    assert response.status_code == 400, response.text
    assert "disabled" in response.json()["message"].lower()


def test_service_is_constructible():
    assert RegistrationSyncService is not None
