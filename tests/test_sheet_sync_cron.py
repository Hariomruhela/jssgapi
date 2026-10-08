from __future__ import annotations

import time
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.security import create_access_token, hash_password
from app.main import app
from app.models.family import FamilyMember
from app.models.member import Member
from app.models.professional import ProfessionalInformation
from app.models.user import Role, User
from app.services.google_sheets_service import SheetTable
from app.services.profile_service import PROFILE_FIELDS
from app.services.registration_sync_service import RegistrationSyncService
from app.services.sheet_columns import normalize_column_name
from tests.test_registration_sync import (
    HEADERS,
    PASSWORD,
    SHEET_ID,
    _call,
    _fresh_phone,
)

# HEADERS positions used below.
_FULL_NAME = 1
_ADDRESS = 9
_CITY = 10
_PHONE = 12

_created_user_ids: list[object] = []


# --- fixtures -----------------------------------------------------------


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
            full_name="Sheet Sync Admin",
            is_email_verified=True,
            is_phone_verified=True,
            role_id=role.id,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        _created_user_ids.append(user.id)
        return create_access_token(subject=str(user.id))


# --- helpers ------------------------------------------------------------


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


def _cells(
    name: str,
    phone: str,
    *,
    address: str = "",
    city: str = "",
    extra: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """One sheet row in HEADERS order, plus values for any extra headers."""
    cells = [""] * len(HEADERS)
    cells[0] = "t"
    cells[_FULL_NAME] = name
    cells[_ADDRESS] = address
    cells[_CITY] = city
    cells[_PHONE] = phone
    return tuple(cells) + extra


def _sheet(
    rows: list[tuple[str, ...]], extra_headers: tuple[str, ...] = ()
) -> SheetTable:
    headers = list(HEADERS) + list(extra_headers)
    return SheetTable(
        spreadsheet_id=SHEET_ID,
        sheet_name="Form Responses 8",
        header_row=1,
        data_start_row=2,
        columns=tuple((chr(ord("A") + i), header) for i, header in enumerate(headers)),
        rows=tuple((index + 2, row) for index, row in enumerate(rows)),
    )


def _write(client: TestClient, token: str, table: SheetTable):
    """POST the sync in write mode (kill switch turned on)."""
    with mock.patch(
        "app.services.registration_sync_service.get_settings"
    ) as get_cfg:
        get_cfg.return_value.google_sheets_sync_enabled = True
        return _call(client, token, table, dry_run=False)


def _member_row(phone: str) -> Member | None:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    with Session(engine) as session:
        return session.execute(
            select(Member).where(Member.contact_phone == phone)
        ).scalar_one_or_none()


def _column_exists(name: str) -> bool:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    with Session(engine) as session:
        row = session.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'members' AND column_name = :name"
            ),
            {"name": name},
        ).first()
    return row is not None


def _cleanup_member(phone: str) -> None:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    with Session(engine) as session:
        member = session.execute(
            select(Member).where(Member.contact_phone == phone)
        ).scalar_one_or_none()
        if member is None:
            return
        user_id = member.user_id
        session.execute(
            delete(FamilyMember).where(FamilyMember.member_id == member.id)
        )
        session.execute(
            delete(ProfessionalInformation).where(
                ProfessionalInformation.member_id == member.id
            )
        )
        session.delete(member)
        session.execute(delete(User).where(User.id == user_id))
        session.commit()


# --- column names (pure) --------------------------------------------------


def test_normalize_column_name_is_deterministic_and_safe():
    assert normalize_column_name("Member Occupation / Profession") == (
        "member_occupation_profession"
    )
    assert normalize_column_name("City / Area") == "city_area"
    assert normalize_column_name("  Full Name  ") == "full_name"
    assert normalize_column_name("Member\u2019s Name") == "member_s_name"
    assert normalize_column_name("Mobile_Number") == "mobile_number"
    assert normalize_column_name("123 Ref") == "col_123_ref"
    assert normalize_column_name("") == ""
    assert normalize_column_name("   ") == ""
    assert normalize_column_name("!!!") == ""
    assert normalize_column_name("___") == ""
    # case / punctuation differences collapse onto the same column
    assert (
        normalize_column_name("Member Ward")
        == normalize_column_name("member ward")
        == normalize_column_name("Member-Ward!")
    )
    # PostgreSQL's 63 byte identifier limit
    long_name = normalize_column_name("Member Ward " + "x" * 120)
    assert 0 < len(long_name) <= 63
    assert long_name[0].isalpha() or long_name[0] == "_"


# --- automatic columns -----------------------------------------------------


def test_dry_run_reports_new_columns_without_creating_them(
    client: TestClient, admin_token: str
):
    ward_header = f"Member Ward Note {int(time.time())}"
    column = normalize_column_name(ward_header)
    phone = _fresh_phone()
    table = _sheet(
        [_cells("Preview Person", phone, extra=("Ward 9",))], (ward_header,)
    )
    assert _column_exists(column) is False

    response = _call(client, admin_token, table)  # dry_run by default
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["unmapped_columns"] == [
        "Definitely Not A Real Column",
        ward_header,
    ]
    assert column in data["new_columns"]
    assert _column_exists(column) is False, "a dry run must not run DDL"


def test_new_column_cap_is_enforced(
    client: TestClient, admin_token: str, monkeypatch
):
    from app.services import sheet_columns

    monkeypatch.setattr(sheet_columns, "MAX_NEW_COLUMNS_PER_RUN", 1)
    stamp = int(time.time() * 1000) % 10**9
    first, second = f"Member Ward Note {stamp} A", f"Member Ward Note {stamp} B"
    phone = _fresh_phone()
    table = _sheet(
        [_cells("Cap Person", phone, extra=("x", "y"))], (first, second)
    )
    response = _call(client, admin_token, table)  # dry run: nothing is created
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["new_columns"]) == 1, data["new_columns"]
    assert data["unmapped_columns"] == [
        "Definitely Not A Real Column",
        first,
        second,
    ]


def test_write_creates_column_updates_member_and_never_duplicates(
    client: TestClient, admin_token: str
):
    """Insert once, then merge: never a second member for one mobile number."""
    ward_header = f"Member Ward Note {int(time.time() * 1000) % 10**9}"
    column = normalize_column_name(ward_header)
    phone = _fresh_phone()
    name = f"Upsert Person {int(time.time() * 1000) % 10**6}"

    try:
        # --- run 1: brand new member + brand new column ------------------
        table = _sheet(
            [
                _cells(
                    name,
                    phone,
                    address="1 Old Street",
                    city="Indore",
                    extra=("Ward 7",),
                )
            ],
            (ward_header,),
        )
        response = _write(client, admin_token, table)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["failed"] == 0, data["errors"]
        assert data["inserted"] == 1, data
        assert data["updated"] == 0
        assert column in data["new_columns"], data["new_columns"]

        member = _member_row(phone)
        assert member is not None
        assert member.address_line == "1 Old Street"
        _created_user_ids.append(member.user_id)
        engine = create_engine(_psycopg_url(), poolclass=NullPool)
        with Session(engine) as session:
            stored_ward = session.execute(
                text(f'SELECT "{column}" FROM members WHERE id = :id'),
                {"id": member.id},
            ).scalar_one()
            profile = dict(
                session.execute(
                    select(Member.profile_data).where(Member.id == member.id)
                ).scalar_one()
                or {}
            )
        assert stored_ward == "Ward 7"
        # Unknown columns never leak into the profile bag.
        assert column not in profile
        assert set(profile) <= set(PROFILE_FIELDS)

        # --- run 2: same mobile number, changed values -------------------
        table = _sheet(
            [
                _cells(
                    "Renamed Person",
                    phone,
                    address="",  # blank: must NOT clear the stored address
                    city="Indore",
                    extra=("Ward 9",),
                )
            ],
            (ward_header,),
        )
        response = _write(client, admin_token, table)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["failed"] == 0, data["errors"]
        assert data["inserted"] == 0, data
        assert data["updated"] == 1, data
        assert data["new_columns"] == [], "the column already exists"
        actions = {row["row"]: row["action"] for row in data["rows"]}
        assert actions[2] == "update"

        with Session(engine) as session:
            users = (
                session.execute(select(User.id).where(User.phone_number == phone))
                .scalars()
                .all()
            )
            assert len(users) == 1, "no duplicate user for one mobile number"
            member = session.execute(
                select(Member).where(Member.contact_phone == phone)
            ).scalar_one()
            # Names are split like the insert path does (first + last).
            assert member.first_name == "Renamed", "changed value merged"
            assert member.last_name == "Person"
            assert member.address_line == "1 Old Street", "blank cell kept the value"
            stored_ward = session.execute(
                text(f'SELECT "{column}" FROM members WHERE id = :id'),
                {"id": member.id},
            ).scalar_one()
            assert stored_ward == "Ward 9", "extra column merged"
            profile = dict(member.profile_data or {})
            assert profile["full_name"] == "Renamed Person"
            assert profile["city"] == "Indore", "blank cell kept the profile value"
            assert set(profile) <= set(PROFILE_FIELDS)
    finally:
        _cleanup_member(phone)


def test_write_never_writes_managed_columns(
    client: TestClient, admin_token: str
):
    """"User ID" normalises to members.user_id - never written from a sheet."""
    header = "User ID"
    column = normalize_column_name(header)
    assert column == "user_id"
    phone = _fresh_phone()
    table = _sheet(
        [_cells("Managed Column", phone, extra=("should-not-be-used",))],
        (header,),
    )
    try:
        response = _write(client, admin_token, table)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert header in data["unmapped_columns"]
        assert column not in data["new_columns"], "managed columns are never created"
        assert data["inserted"] == 1, data
        member = _member_row(phone)
        assert member is not None
        _created_user_ids.append(member.user_id)
        engine = create_engine(_psycopg_url(), poolclass=NullPool)
        with Session(engine) as session:
            user_id = session.execute(
                text("SELECT user_id FROM members WHERE id = :id"),
                {"id": member.id},
            ).scalar_one()
        assert str(user_id) != "should-not-be-used"
    finally:
        _cleanup_member(phone)


def test_row_failure_counts_as_failed_and_rolls_back(
    client: TestClient, admin_token: str
):
    """One exploding row must be counted, reported and fully rolled back."""
    phone = _fresh_phone()
    table = _sheet([_cells("Boom Person", phone)])
    with (
        mock.patch(
            "app.services.registration_sync_service.get_settings"
        ) as get_cfg,
        mock.patch.object(
            RegistrationSyncService, "_insert_row", side_effect=RuntimeError("boom")
        ),
    ):
        get_cfg.return_value.google_sheets_sync_enabled = True
        response = _call(client, admin_token, table, dry_run=False)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["failed"] == 1, data
    assert data["inserted"] == 0
    assert "boom" in data["rows"][0]["reason"]
    assert _member_row(phone) is None, "the failed row must be rolled back"
