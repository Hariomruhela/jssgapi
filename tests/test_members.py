from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.profile_service import PROFILE_FIELDS

PROFILE_FIELD_SET = set(PROFILE_FIELDS)
EXISTING_MEMBER_FIELDS = {
    "id",
    "user_id",
    "group_id",
    "first_name",
    "last_name",
    "membership_status",
    "user",
    "family_members",
    "professional_info",
    "created_at",
    "updated_at",
}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _assert_items(items: list[dict]) -> None:
    for item in items:
        missing = PROFILE_FIELD_SET - item.keys()
        assert not missing, f"missing profile fields: {sorted(missing)}"
        assert EXISTING_MEMBER_FIELDS <= item.keys()
        for field in PROFILE_FIELD_SET:
            value = item[field]
            assert value is None or isinstance(
                value, str
            ), f"{field} is neither null nor a string: {value!r}"


def test_members_list_returns_every_profile_field(client: TestClient):
    response = client.get("/api/v1/members", params={"page_size": 100})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["message"] == "Members fetched successfully"
    assert body["pagination"]["page"] == 1

    items = body["data"]
    assert len(items) == min(body["pagination"]["total"], 100)
    _assert_items(items)


def test_members_list_search_returns_every_profile_field(client: TestClient):
    listing = client.get("/api/v1/members", params={"page_size": 100})
    assert listing.status_code == 200, listing.text
    sample = listing.json()["data"]
    if not sample:
        pytest.skip("no members in the database to search for")

    response = client.get("/api/v1/members", params={"query": sample[0]["first_name"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert "pagination" not in body
    _assert_items(body["data"])


def test_members_list_keeps_pagination_envelope(client: TestClient):
    response = client.get("/api/v1/members", params={"page": 1, "page_size": 2})
    assert response.status_code == 200, response.text
    pagination = response.json()["pagination"]
    assert pagination["page"] == 1
    assert pagination["page_size"] == 2
    assert pagination["total_pages"] >= 1
