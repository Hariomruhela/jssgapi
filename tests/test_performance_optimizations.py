from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.constants import RoleName
from app.core.scope import AccessScope, get_access_scope, get_admin_scope
from app.models.event import Event
from app.models.jain_tirth import JainTirth
from app.models.member import Member
from app.models.user import Role, User
from app.repositories.event_image_repository import EventImageRepository
from app.repositories.jain_tirth_image_repository import JainTirthImageRepository
from app.schemas.celebration import BirthdayOut, CelebrationPersonOut
from app.services.celebration_service import (
    CelebrationService,
    clear_celebration_cache,
)
from app.services.profile_service import ProfileService
from app.services.sheet_groups_service import (
    clear_sheet_groups_cache,
    read_sheet_groups,
)


@pytest.mark.asyncio
async def test_event_images_batch_count():
    session = AsyncMock()
    mock_result = MagicMock()
    id1, id2, id3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    mock_result.all.return_value = [(id1, 3), (id2, 1)]
    session.execute.return_value = mock_result

    repo = EventImageRepository(session)
    counts = await repo.batch_count_for_events([id1, id2, id3])

    assert counts[id1] == 3
    assert counts[id2] == 1
    assert counts[id3] == 0  # not in DB -> 0


@pytest.mark.asyncio
async def test_jain_tirth_images_batch_count():
    session = AsyncMock()
    mock_result = MagicMock()
    id1, id2 = uuid.uuid4(), uuid.uuid4()
    mock_result.all.return_value = [(id1, 5)]
    session.execute.return_value = mock_result

    repo = JainTirthImageRepository(session)
    counts = await repo.batch_count_for_tirths([id1, id2])

    assert counts[id1] == 5
    assert counts[id2] == 0


def test_sheet_groups_caching():
    clear_sheet_groups_cache()

    with patch(
        "app.services.sheet_groups_service.get_settings"
    ) as mock_settings, patch(
        "app.services.sheet_groups_service.GoogleSheetsService"
    ) as mock_sheets_cls:
        settings_instance = MagicMock()
        settings_instance.google_sheets_spreadsheet_id = "test-sheet-id"
        settings_instance.google_sheets_sheet_name = "Sheet1"
        settings_instance.google_sheets_header_row = 1
        settings_instance.google_sheets_data_start_row = 2
        mock_settings.return_value = settings_instance

        mock_sheets = MagicMock()
        mock_sheets_cls.return_value = mock_sheets
        mock_table = MagicMock()
        mock_table.columns = [("A", "Social Group Name")]
        mock_table.rows = [(2, ("Group Alpha",))]
        mock_sheets.read_table.return_value = mock_table

        # First read - should hit GoogleSheetsService.read_table
        res1 = read_sheet_groups()
        assert len(res1) == 1
        assert res1[0]["name"] == "Group Alpha"
        assert mock_sheets.read_table.call_count == 1

        # Second read - should hit in-memory cache, read_table NOT called again
        res2 = read_sheet_groups()
        assert len(res2) == 1
        assert res2[0]["name"] == "Group Alpha"
        assert mock_sheets.read_table.call_count == 1  # Still 1!


@pytest.mark.asyncio
async def test_celebration_service_caching():
    clear_celebration_cache()
    session = AsyncMock()
    service = CelebrationService(session)

    # Mock candidate retrieval
    dummy_member = Member(
        id=uuid.uuid4(),
        first_name="Test",
        last_name="User",
        date_of_birth=date(1990, 10, 26),
        is_deleted=False,
    )
    dummy_member.group = None
    dummy_member.location = None
    dummy_member.family_members = []
    dummy_member.professional_info = []
    dummy_member.profile_data = {}

    service._candidates = AsyncMock(return_value=[dummy_member])

    scope = AccessScope(unrestricted=True)
    target = date(2026, 10, 26)

    # First call - queries candidates
    birthdays1 = await service.birthdays(scope, target)
    assert len(birthdays1) == 1
    assert service._candidates.call_count == 1

    # Second call - returns cached result without querying candidates
    birthdays2 = await service.birthdays(scope, target)
    assert len(birthdays2) == 1
    assert service._candidates.call_count == 1  # Still 1!


@pytest.mark.asyncio
async def test_scope_caching_on_user():
    session = AsyncMock()
    group_id = uuid.uuid4()
    admin_user = User(
        id=uuid.uuid4(),
        full_name="Group Admin",
        phone_number="+919999999999",
        password_hash="hash",
        group_id=group_id,
    )
    admin_user.role = Role(name="GROUP_ADMIN")

    # get_admin_scope should return group_id directly and cache on user
    scope1 = await get_admin_scope(session, admin_user)
    assert scope1 == group_id
    assert admin_user._cached_admin_scope == group_id
    assert session.execute.call_count == 0  # No DB query needed!

    # get_access_scope should return group_id directly without querying Member
    access_scope = await get_access_scope(session, admin_user)
    assert access_scope.unrestricted is False
    assert access_scope.group_id == group_id
    assert admin_user._cached_access_scope == access_scope
    assert session.execute.call_count == 0  # Still 0 DB queries!


def test_get_profile_dict():
    session = MagicMock()
    service = ProfileService(session)

    member = Member(
        id=uuid.uuid4(),
        first_name="Ramesh",
        last_name="Jain",
        date_of_birth=date(1985, 5, 15),
        contact_phone="+919876543210",
        contact_email="ramesh@example.com",
        profile_data={"city": "Indore", "full_name": "Ramesh Jain"},
    )
    member.user = None
    member.group = None
    member.location = None
    member.family_members = []
    member.professional_info = []

    profile_dict = service.get_profile_dict(member)
    profile_obj = service.build_profile(member)

    assert profile_dict["full_name"] == profile_obj.full_name
    assert profile_dict["city"] == profile_obj.city
    assert profile_dict["phone_number"] == profile_obj.phone_number
