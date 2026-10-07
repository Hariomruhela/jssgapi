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
from app.models.family import FamilyMember
from app.models.member import Member
from app.models.professional import ProfessionalInformation
from app.models.user import Role, User
from app.services.google_sheets_service import SheetTable
from app.services.profile_service import PROFILE_FIELDS
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
        role = session.execute(
            select(Role).where(Role.name == "SUPER_ADMIN")
        ).scalar_one()
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


def test_spouse_mobile_is_normalised_to_e164():
    """A 10-digit sheet value must be stored as +91XXXXXXXXXX.

    The sheet records the spouse mobile without a country code, which used to
    land in family_members.contact_phone and profile_data as a bare 10-digit
    string while the member's own phone was E.164.
    """
    headers = [h for _label, h in _table([]).columns]
    row = _row(
        "t",
        "Asha Verma",
        "14/03/1980",
        "06/12/1985",
        "02/11/2010",
        "",
        "Verma Spouse",
        "9876543210",
        "",
        "1 Test Street",
        "Indore",
        "Vijay Nagar",
        "+919999999999",
        "asha@example.com",
        "B.Tech",
        "Engineer",
        "Acme",
        "Sec",
        "JSSG",
        "Health",
        "",
    )

    with mock.patch("app.services.registration_sync_service.get_settings"):
        service = RegistrationSyncService(None)
        parsed = service._parse_row(2, row, headers)

    assert parsed.values["spouse_mobile"] == "+919876543210"
    assert parsed.phone == "+919999999999"


def test_unparsable_spouse_mobile_is_kept_as_entered():
    """A malformed spouse number must not fail the row; it is only a warning."""
    headers = [h for _label, h in _table([]).columns]
    row = _row(
        "t",
        "Asha Verma",
        "14/03/1980",
        "06/12/1985",
        "02/11/2010",
        "",
        "Verma Spouse",
        "not-a-number",
        "",
        "1 Test Street",
        "Indore",
        "Vijay Nagar",
        "+919999999999",
        "asha@example.com",
        "B.Tech",
        "Engineer",
        "Acme",
        "Sec",
        "JSSG",
        "Health",
        "",
    )

    with mock.patch("app.services.registration_sync_service.get_settings"):
        service = RegistrationSyncService(None)
        parsed = service._parse_row(2, row, headers)

    assert parsed.errors == []
    assert parsed.values["spouse_mobile"] == "not-a-number"
    assert "spouse_mobile" in {w.column for w in parsed.warnings}


def test_endpoint_requires_authentication(client: TestClient):
    response = client.post(ENDPOINT, json={"dry_run": True})
    assert response.status_code == 401


def test_write_mode_blocked_by_kill_switch(client: TestClient, admin_token: str):
    table = _table(
        [_row("t", "Blocked", "", "", "", "", "", "", "", "", "", "", "9876500002")]
    )
    with mock.patch("app.services.registration_sync_service.get_settings") as get_cfg:
        get_cfg.return_value.google_sheets_sync_enabled = False
        response = _call(client, admin_token, table, dry_run=False)
    assert response.status_code == 400, response.text
    assert "disabled" in response.json()["message"].lower()


def test_service_is_constructible():
    assert RegistrationSyncService is not None


def test_placeholders_are_dropped_not_stored():
    from app.services.registration_sync_service import is_placeholder

    for value in ("NA", "na", "Nil", "nil", "N/A", "-", "Same as above", "None"):
        assert is_placeholder(value) is True, value
    for value in ("Naveen", "N/A College", "Nancy", "xyz"):
        assert is_placeholder(value) is False, value


def test_dry_run_reports_placeholder_warnings(client: TestClient, admin_token: str):
    table = _table(
        [
            _row(
                "t",
                "Placeholder Row",
                "1/1/1990",
                "",
                "",
                "",
                "NA",
                "",
                "",
                "",
                "",
                "",
                "9876500009",
                "",
                "",
                "NA",
                "",
                "",
                "",
                "",
                "",
            )
        ]
    )
    response = _call(client, admin_token, table)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["inserted"] == 1
    dropped = {w["column"] for w in data["warnings"]}
    assert "spouse_name" in dropped
    assert "member_occupation" in dropped


def test_missing_fields_stay_null_and_placeholders_are_dropped():
    """A field the sheet left blank must stay null, never a literal "NA".

    Storing "NA" would be indistinguishable from a real value and would create
    a family_members row literally named "NA", so the importer leaves the key
    absent and the API reports null.
    """
    headers = [h for _label, h in _table([]).columns]
    row = _row(
        "t",
        "Asha Verma",
        "14/03/1980",
        "",
        "",
        "",
        "NA",
        "9876543210",
        "",
        "1 Test Street",
        "Indore",
        "Vijay Nagar",
        "+919999999999",
        "asha@example.com",
        "B.Tech",
        ".",
        "",
        "JSSG",
        "Health",
        "",
    )

    with mock.patch("app.services.registration_sync_service.get_settings"):
        service = RegistrationSyncService(None)
        parsed = service._parse_row(2, row, headers)

    values = parsed.values
    # Blank cells are not keys at all.
    assert "anniversary_date" not in values
    assert "son_name" not in values
    # Placeholder cells are dropped, not stored as "NA"/"Nil".
    assert "spouse_name" not in values
    assert "member_occupation" not in values
    # Real values survive untouched.
    assert values["full_name"] == "Asha Verma"
    assert values["city"] == "Indore"
    dropped = {w.column for w in parsed.warnings}
    assert {"spouse_name", "member_occupation"} <= dropped


def test_write_mode_actually_fills_profile_data(client: TestClient, admin_token: str):
    """Regression: a write must persist profile_data, not just user+member.

    A lazy-loaded relationship inside the async session used to raise
    MissingGreenlet, which rolled the row back yet still left the user and
    member behind with an empty profile.
    """
    phone = _fresh_phone()
    table = _table(
        [
            _row(
                "t",
                "Regression Member",
                # Unambiguous dd/mm so the day-first default is provable.
                "14/03/1980",
                "06/12/1985",
                "02/11/2010",
                "https://drive.google.com/open?id=abc",
                "Regression Spouse",
                "9998887776",
                "1",
                "1 Test Street",
                "Indore",
                "Vijay Nagar",
                phone,
                "regression@example.com",
                "B.Tech",
                "Engineer",
                "Acme",
                "Secretary",
                "JSSG",
                "Health",
                "https://drive.google.com/open?id=xyz",
            )
        ]
    )
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    with Session(engine) as session:
        session.execute(
            User.__table__.update()
            .where(User.full_name == "Regression Member")
            .values(is_phone_verified=False)
        )
        session.commit()
    try:
        with mock.patch(
            "app.services.registration_sync_service.get_settings"
        ) as get_cfg:
            get_cfg.return_value.google_sheets_sync_enabled = True
            response = _call(client, admin_token, table, dry_run=False)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["inserted"] == 1, data["errors"]
        assert data["failed"] == 0, data["errors"]

        with Session(engine) as session:
            member = session.execute(
                select(Member).where(Member.contact_phone == phone)
            ).scalar_one()
            user_id = member.user_id
            _created_user_ids.append(user_id)
            profile = member.profile_data
            assert profile["full_name"] == "Regression Member"
            assert profile["city"] == "Indore"
            assert profile["spouse_name"] == "Regression Spouse"
            assert profile["member_dob"] == "1980-03-14"
            # Daughter was never filled in on the sheet, so no key is written
            # and the API reports null rather than a literal "NA".
            assert "daughter_name" not in profile
            assert "NA" not in profile.values()
            # Every key that IS written must be a real profile field.
            assert set(profile) <= set(PROFILE_FIELDS)
            family = (
                session.execute(
                    select(FamilyMember).where(FamilyMember.member_id == member.id)
                )
                .scalars()
                .all()
            )
            assert [f.relationship_type for f in family] == ["spouse"]
            assert not any(f.name.upper() in {"NA", "NIL"} for f in family)
            professional = (
                session.execute(
                    select(ProfessionalInformation).where(
                        ProfessionalInformation.member_id == member.id
                    )
                )
                .scalars()
                .all()
            )
            assert len(professional) == 1
            assert professional[0].company_name == "Acme"
    finally:
        with Session(engine) as session:
            member = session.execute(
                select(Member).where(Member.contact_phone == phone)
            ).scalar_one_or_none()
            if member is not None:
                # family_members.member_id is NOT NULL, so children go first.
                session.execute(
                    delete(FamilyMember).where(FamilyMember.member_id == member.id)
                )
                session.execute(
                    delete(ProfessionalInformation).where(
                        ProfessionalInformation.member_id == member.id
                    )
                )
                session.delete(member)
                session.execute(delete(User).where(User.id == member.user_id))
            session.commit()
