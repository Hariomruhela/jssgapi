from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.constants import MemberStatus
from app.core.exceptions import UnauthorizedException
from app.main import app
from app.models.group import SocialGroup
from app.models.member import Member
from app.models.member_group import MemberGroupMembership
from app.models.user import User


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _mock_external_auth(monkeypatch):
    """Fake MSG91 send-OTP and Firebase ID-token verification."""

    async def _fake_send_otp(mobile: str) -> str:
        return mobile

    def _fake_verify_id_token(id_token: str) -> dict:
        if id_token == "invalid-token":
            raise UnauthorizedException("Invalid or expired Firebase ID token")
        if id_token == "no-phone-token":
            return {"uid": "uid-no-phone"}
        return {"uid": f"uid-{id_token}", "phone_number": id_token}

    monkeypatch.setattr("app.services.auth_service.msg91_send_otp", _fake_send_otp)
    monkeypatch.setattr(
        "app.services.auth_service.firebase_verify_id_token", _fake_verify_id_token
    )


def _psycopg_url() -> str:
    url = get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url


def _next_phone(variant: int = 0) -> str:
    suffix = str(int(time.time() * 1000) + variant)[-9:]
    return f"+916{suffix}"


def _next_email() -> str:
    return f"authtest_{int(time.time() * 1000)}@test.local"


_MISSING = object()


def _register(
    client,
    phone: str,
    email: object = _MISSING,
    password: str | None = "V3ryStr0ng!Pass",
    group_id: object = None,
    group_name: str | None = None,
    id_token: str | None = None,
) -> dict:
    body: dict[str, object] = {
        "full_name": "Auth Test User",
        "email": None if email is _MISSING else email,
        "password": password,
        "phone_number": phone.removeprefix("+91"),
        "confirm_password": password,
    }
    if group_id is not None:
        body["group_id"] = str(group_id)
    if group_name is not None:
        body["group_name"] = group_name
    if id_token is not None:
        body["id_token"] = id_token
    resp = client.post("/api/v1/auth/register", json=body)
    return {"status": resp.status_code, "body": resp.json()}


def _cleanup(emails: list[str], phones: list[str]) -> None:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            u_ids = list(conn.execute(select(User.id).where(
                (User.email.in_(emails)) | (User.phone_number.in_(phones))
            )).scalars())
            if u_ids:
                m_ids = list(conn.execute(select(Member.id).where(Member.user_id.in_(u_ids))).scalars())
                if m_ids:
                    conn.execute(delete(MemberGroupMembership).where(MemberGroupMembership.member_id.in_(m_ids)))
                    conn.execute(delete(Member).where(Member.id.in_(m_ids)))
                conn.execute(delete(User).where(User.id.in_(u_ids)))
            conn.commit()
    finally:
        engine.dispose()


def _approve_user(phone: str, status="APPROVED", reason=None) -> None:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    with engine.connect() as conn:
        u = conn.execute(select(User.id).where(User.phone_number == phone)).scalar_one_or_none()
        if u:
            is_active = (status == "APPROVED")
            conn.execute(update(User).where(User.id == u).values(is_active=is_active))
            conn.execute(update(Member).where(Member.user_id == u).values(
                membership_status=status,
                rejection_reason=reason
            ))
            conn.commit()
    engine.dispose()


def test_register_with_email_succeeds(client):
    phone = _next_phone()
    email = _next_email()
    result = _register(client, phone, email=email)
    assert result["status"] == 200, result["body"]
    body = result["body"]
    assert body["user"]["email"] == email
    assert body["user"]["phone_number"] == phone
    assert body["user"]["full_name"] == "Auth Test User"
    assert body["user"]["is_active"] is False
    assert body["tokens"] is None
    assert "pending approval" in body["message"].lower()
    assert "password_hash" not in body["user"]
    _cleanup([email], [phone])


def test_register_without_email_succeeds(client):
    phone = _next_phone()
    result = _register(client, phone)
    assert result["status"] == 200, result["body"]
    body = result["body"]
    assert body["user"]["email"] is None
    assert body["user"]["phone_number"] == phone
    assert body["user"]["is_active"] is False
    assert body["tokens"] is None
    assert "pending approval" in body["message"].lower()
    _cleanup([], [phone])


def test_register_with_null_email_succeeds(client):
    phone = _next_phone()
    result = _register(client, phone, email=None)
    assert result["status"] == 200, result["body"]
    assert result["body"]["user"]["email"] is None
    _cleanup([], [phone])


def test_register_with_empty_email_succeeds(client):
    phone = _next_phone()
    result = _register(client, phone, email="")
    assert result["status"] == 200, result["body"]
    assert result["body"]["user"]["email"] is None
    _cleanup([], [phone])


def test_register_with_blank_email_succeeds_and_saves_null(client):
    phone = _next_phone()
    result = _register(client, phone, email="   ")
    assert result["status"] == 200, result["body"]
    assert result["body"]["user"]["email"] is None
    _cleanup([], [phone])


def test_register_multiple_users_without_email_succeeds(client):
    phones = [_next_phone(1), _next_phone(2)]
    for phone in phones:
        result = _register(client, phone)
        assert result["status"] == 200, result["body"]
        assert result["body"]["user"]["email"] is None
    _cleanup([], phones)


def test_register_without_password_is_rejected(client):
    phone = _next_phone()
    result = _register(client, phone, password=None)
    assert result["status"] == 422
    _cleanup([], [phone])


def test_register_duplicate_phone_is_rejected(client):
    phone = _next_phone()
    first = _register(client, phone, email=_next_email())
    assert first["status"] == 200, first["body"]
    second = _register(client, phone, email=_next_email())
    assert second["status"] == 409
    assert second["body"] == {
        "success": False,
        "message": "This phone number is already registered.",
        "detail": "This phone number is already registered.",
        "error_code": "ALREADY_EXISTS",
    }
    _cleanup([], [phone])


def test_register_duplicate_email(client):
    email = _next_email()
    phone = _next_phone()
    first = _register(client, phone, email=email)
    assert first["status"] == 200, first["body"]
    second_phone = _next_phone()
    second = _register(client, second_phone, email=email)
    assert second["status"] == 409
    _cleanup([email], [phone, second_phone])


def test_register_invalid_phone(client):
    result = _register(client, "not-a-phone")
    assert result["status"] == 422


def test_register_accepts_optional_id_token(client):
    phone = _next_phone()
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Auth Test User",
            "email": None,
            "password": "V3ryStr0ng!Pass",
            "phone_number": phone.removeprefix("+91"),
            "confirm_password": "V3ryStr0ng!Pass",
            "id_token": "legacy-token",
        },
    )
    assert resp.status_code == 200, resp.text
    _cleanup([], [phone])


def test_send_register_otp_succeeds(client):
    phone = _next_phone()
    resp = client.post("/api/v1/auth/otp/send-register", json={"phone": phone})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["req_id"] == phone


def test_send_register_otp_for_existing_user(client):
    phone = _next_phone()
    assert _register(client, phone, email=_next_email())["status"] == 200
    resp = client.post("/api/v1/auth/otp/send-register", json={"phone": phone})
    assert resp.status_code == 409
    _cleanup([], [phone])


@pytest.mark.parametrize(
    "bad_email",
    ["not-an-email", "abc", "test", "hello@", "@gmail.com"],
)
def test_register_invalid_email(client, bad_email):
    result = _register(client, _next_phone(), email=bad_email)
    assert result["status"] == 422


def test_login_with_phone_succeeds(client):
    phone = _next_phone()
    email = _next_email()
    assert _register(client, phone, email=email)["status"] == 200
    # Pending user cannot login - 403 Forbidden
    pending_resp = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "V3ryStr0ng!Pass"},
    )
    assert pending_resp.status_code == 403
    assert "pending approval" in pending_resp.json()["detail"].lower()

    # Once approved, login succeeds
    _approve_user(phone)
    resp = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "V3ryStr0ng!Pass"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["phone_number"] == phone
    assert body["tokens"]["access_token"]
    _cleanup([email], [phone])


def test_login_with_firebase_id_token_succeeds(client):
    phone = _next_phone()
    email = _next_email()
    assert _register(client, phone, email=email)["status"] == 200
    _approve_user(phone)
    resp = client.post("/api/v1/auth/login", json={"id_token": phone})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["phone_number"] == phone
    assert body["tokens"]["access_token"]
    _cleanup([email], [phone])


def test_login_with_firebase_id_token_unknown_phone(client):
    resp = client.post(
        "/api/v1/auth/login", json={"id_token": "+916000000000"}
    )
    assert resp.status_code == 401


def test_login_otp_with_firebase_id_token_succeeds_and_refreshes(client):
    phone = _next_phone()
    email = _next_email()
    assert _register(client, phone, email=email)["status"] == 200
    _approve_user(phone)
    resp = client.post("/api/v1/auth/login/otp", json={"id_token": phone})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["phone_number"] == phone
    assert body["user"]["email"] == email
    assert body["tokens"]["access_token"]
    assert body["tokens"]["refresh_token"]

    refresh = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": body["tokens"]["refresh_token"]},
    )
    assert refresh.status_code == 200, refresh.text
    _cleanup([email], [phone])


def test_login_otp_unknown_phone_returns_registration_message(client):
    resp = client.post(
        "/api/v1/auth/login/otp", json={"id_token": _next_phone()}
    )
    assert resp.status_code == 404
    assert resp.json() == {
        "success": False,
        "message": "इस मोबाइल नंबर से खाता नहीं मिला। कृपया पहले पंजीकरण करें।",
        "error_code": "USER_NOT_FOUND",
    }


def test_login_otp_rejects_password_field(client):
    resp = client.post(
        "/api/v1/auth/login/otp",
        json={"id_token": _next_phone(), "password": "V3ryStr0ng!Pass"},
    )
    assert resp.status_code == 422


def test_login_otp_with_invalid_firebase_id_token(client):
    resp = client.post(
        "/api/v1/auth/login/otp", json={"id_token": "invalid-token"}
    )
    assert resp.status_code == 401
    assert resp.json() == {
        "success": False,
        "message": "Invalid or expired Firebase ID token",
        "error_code": "UNAUTHORIZED",
    }


def test_login_otp_token_without_phone(client):
    resp = client.post(
        "/api/v1/auth/login/otp", json={"id_token": "no-phone-token"}
    )
    assert resp.status_code == 401
    assert resp.json()["message"] == (
        "Firebase token does not contain a verified phone number"
    )


def test_login_with_invalid_firebase_id_token(client):
    resp = client.post("/api/v1/auth/login", json={"id_token": "invalid-token"})
    assert resp.status_code == 401


def test_login_incorrect_password(client):
    phone = _next_phone()
    email = _next_email()
    assert _register(client, phone, email=email)["status"] == 200
    resp = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "WrongPass123"},
    )
    assert resp.status_code == 401
    _cleanup([email], [phone])


def test_login_nonexistent_phone(client):
    resp = client.post(
        "/api/v1/auth/login",
        json={"phone_number": "+916000000000", "password": "V3ryStr0ng!Pass"},
    )
    assert resp.status_code == 401


def test_protected_endpoint_with_valid_jwt(client):
    phone = _next_phone()
    email = _next_email()
    result = _register(client, phone, email=email)
    assert result["status"] == 200, result["body"]
    _approve_user(phone)
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "V3ryStr0ng!Pass"},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["tokens"]["access_token"]
    resp = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["phone_number"] == phone
    _cleanup([email], [phone])


def test_protected_endpoint_without_jwt(client):
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401


def _forgot_password(
    client, phone: str, new_password: str, confirm_password: str
) -> dict:
    resp = client.post(
        "/api/v1/auth/forgot-password",
        json={
            "phone_number": phone,
            "new_password": new_password,
            "confirm_password": confirm_password,
        },
    )
    return {"status": resp.status_code, "body": resp.json()}


def test_forgot_password_success_then_login_with_new_password(client):
    phone = _next_phone()
    email = _next_email()
    assert _register(client, phone, email=email)["status"] == 200
    _approve_user(phone)

    assert _forgot_password(client, phone, "Changed@99", "Changed@99")["status"] == 200

    new_login = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "Changed@99"},
    )
    assert new_login.status_code == 200, new_login.text

    old_login = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "V3ryStr0ng!Pass"},
    )
    assert old_login.status_code == 401
    _cleanup([email], [phone])


def test_forgot_password_unknown_phone(client):
    result = _forgot_password(client, "+916000000001", "Changed@99", "Changed@99")
    assert result["status"] == 404
    assert result["body"]["message"] == "User not found"


def test_forgot_password_mismatch(client):
    result = _forgot_password(client, "+916000000001", "Changed@99", "Different@99")
    assert result["status"] == 422


def test_forgot_password_invalid_password(client):
    result = _forgot_password(client, "+916000000001", "short", "short")
    assert result["status"] == 422


def test_forgot_password_invalid_phone(client):
    result = _forgot_password(client, "12345", "Changed@99", "Changed@99")
    assert result["status"] == 422


def test_reset_password_with_firebase_token_succeeds(client):
    phone = _next_phone()
    email = _next_email()
    assert _register(client, phone, email=email)["status"] == 200
    _approve_user(phone)

    resp = client.post(
        "/api/v1/auth/reset-password",
        json={
            "phone_number": phone.removeprefix("+91"),
            "password": "Changed@99",
            "confirm_password": "Changed@99",
            "id_token": phone,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "success": True,
        "message": "पासवर्ड सफलतापूर्वक बदला गया",
    }

    new_login = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "Changed@99"},
    )
    assert new_login.status_code == 200, new_login.text
    old_login = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "V3ryStr0ng!Pass"},
    )
    assert old_login.status_code == 401
    _cleanup([email], [phone])


def test_reset_password_unknown_phone_returns_registration_message(client):
    phone = _next_phone()
    resp = client.post(
        "/api/v1/auth/reset-password",
        json={
            "phone_number": phone.removeprefix("+91"),
            "password": "Changed@99",
            "confirm_password": "Changed@99",
            "id_token": phone,
        },
    )
    assert resp.status_code == 404
    assert resp.json() == {
        "success": False,
        "message": "इस मोबाइल नंबर से खाता नहीं मिला। कृपया पहले पंजीकरण करें।",
        "error_code": "USER_NOT_FOUND",
    }


def test_reset_password_short_password_returns_hindi_400(client):
    phone = _next_phone()
    resp = client.post(
        "/api/v1/auth/reset-password",
        json={
            "phone_number": phone.removeprefix("+91"),
            "password": "short",
            "confirm_password": "short",
            "id_token": phone,
        },
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "success": False,
        "message": "पासवर्ड कम से कम 8 अक्षरों का होना चाहिए",
        "error_code": "BAD_REQUEST",
    }


def test_reset_password_mismatch_returns_hindi_400(client):
    phone = _next_phone()
    resp = client.post(
        "/api/v1/auth/reset-password",
        json={
            "phone_number": phone.removeprefix("+91"),
            "password": "Changed@99",
            "confirm_password": "Different@99",
            "id_token": phone,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["message"] == "पासवर्ड और पुष्टि पासवर्ड मेल नहीं खाते"


def test_reset_password_invalid_firebase_token(client):
    phone = _next_phone()
    resp = client.post(
        "/api/v1/auth/reset-password",
        json={
            "phone_number": phone.removeprefix("+91"),
            "password": "Changed@99",
            "confirm_password": "Changed@99",
            "id_token": "invalid-token",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "UNAUTHORIZED"


def _create_test_social_group(name: str = "Test Approval Group") -> tuple[str, str]:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    with engine.connect() as conn:
        existing = conn.execute(select(SocialGroup.id).where(SocialGroup.name == name)).scalar_one_or_none()
        if existing:
            return str(existing), name
        import uuid
        gid = uuid.uuid4()
        conn.execute(
            SocialGroup.__table__.insert().values(
                id=gid,
                name=name,
                status="ACTIVE",
                is_active=True,
                is_deleted=False,
            )
        )
        conn.commit()
    engine.dispose()
    return str(gid), name


def _admin_token(client: TestClient) -> str:
    # Login as default superadmin
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "superadmin@jssg.local", "password": "ChangeMe123!"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["tokens"]["access_token"]


def test_registration_with_group_and_pending_state(client):
    gid, gname = _create_test_social_group()
    phone = _next_phone()
    email = _next_email()
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Pending Member",
            "email": email,
            "password": "V3ryStr0ng!Pass",
            "phone_number": phone.removeprefix("+91"),
            "confirm_password": "V3ryStr0ng!Pass",
            "group_id": gid,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["user"]["is_active"] is False
    assert data["user"]["is_phone_verified"] is True
    assert data["tokens"] is None
    assert "pending approval" in data["message"].lower()

    # Verify database state
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    with engine.connect() as conn:
        u = conn.execute(select(User).where(User.phone_number == phone)).mappings().one()
        assert u["is_active"] is False
        assert u["is_phone_verified"] is True
        assert str(u["group_id"]) == gid

        m = conn.execute(select(Member).where(Member.user_id == u["id"])).mappings().one()
        assert m["membership_status"] == MemberStatus.PENDING.value
        assert str(m["group_id"]) == gid
        assert m["first_name"] == "Pending"
        assert m["last_name"] == "Member"
        assert m["contact_phone"] == phone
        assert m["contact_email"] == email
    engine.dispose()
    _cleanup([email], [phone])


def test_registration_with_group_name(client):
    gid, gname = _create_test_social_group("Group By Name")
    phone = _next_phone()
    email = _next_email()
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Named Group Member",
            "email": email,
            "password": "V3ryStr0ng!Pass",
            "phone_number": phone.removeprefix("+91"),
            "confirm_password": "V3ryStr0ng!Pass",
            "group_name": gname,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["user"]["is_active"] is False
    assert str(data["user"]["group_id"]) == gid
    _cleanup([email], [phone])


def test_login_blocked_when_pending_or_rejected(client):
    phone = _next_phone()
    email = _next_email()
    assert _register(client, phone, email=email)["status"] == 200

    # 1. Blocked when PENDING
    login_resp = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "V3ryStr0ng!Pass"},
    )
    assert login_resp.status_code == 403
    assert login_resp.json()["detail"] == (
        "Your registration is pending approval by the Group Admin. Please wait for approval before logging in."
    )

    # 2. Blocked when REJECTED
    _approve_user(phone, status="REJECTED", reason="Invalid contact details")
    rejected_resp = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "V3ryStr0ng!Pass"},
    )
    assert rejected_resp.status_code == 403
    assert "rejected" in rejected_resp.json()["detail"].lower()
    assert "Invalid contact details" in rejected_resp.json()["detail"]

    # 3. Allowed when APPROVED
    _approve_user(phone, status="APPROVED")
    approved_resp = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "V3ryStr0ng!Pass"},
    )
    assert approved_resp.status_code == 200
    assert approved_resp.json()["tokens"]["access_token"]
    _cleanup([email], [phone])


def test_query_pending_members_and_approval_workflow(client):
    gid, gname = _create_test_social_group("Workflow Test Group")
    token = _admin_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    phone = _next_phone()
    email = _next_email()
    reg_resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Approve Me",
            "email": email,
            "password": "V3ryStr0ng!Pass",
            "phone_number": phone.removeprefix("+91"),
            "confirm_password": "V3ryStr0ng!Pass",
            "group_id": gid,
        },
    )
    assert reg_resp.status_code == 200, reg_resp.text

    # 1. Query pending members via GET /api/v1/members?group_id={gid}&status=PENDING
    resp = client.get(f"/api/v1/members?group_id={gid}&status=PENDING", headers=headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]
    match = next((item for item in items if item.get("phone_number") == phone), None)
    assert match is not None, f"Pending member not found in list: {items}"
    # Verify required fields
    assert "member_id" in match and match["member_id"] is not None
    assert "user_id" in match and match["user_id"] is not None
    assert match["full_name"] == "Approve Me"
    assert match["phone_number"] == phone
    assert match["email"] == email
    assert "created_at" in match and match["created_at"] is not None
    assert match["status"] == "PENDING"
    member_id = match["member_id"]

    # Also test GET /api/v1/groups/{gid}/members?status=PENDING
    group_members_resp = client.get(f"/api/v1/groups/{gid}/members?status=PENDING", headers=headers)
    assert group_members_resp.status_code == 200
    gm_items = group_members_resp.json()["data"]
    gm_match = next((item for item in gm_items if item.get("phone_number") == phone), None)
    assert gm_match is not None
    assert gm_match["member_id"] == member_id
    assert gm_match["user_id"] is not None
    assert gm_match["status"] == "PENDING"

    # 2. Reject member via POST /api/v1/members/{member_id}/approve
    reject_resp = client.post(
        f"/api/v1/members/{member_id}/approve",
        headers=headers,
        json={"status": "REJECTED", "rejection_reason": "Missing documents"},
    )
    assert reject_resp.status_code == 200, reject_resp.text
    assert reject_resp.json()["data"]["membership_status"] == "REJECTED"
    assert reject_resp.json()["data"]["rejection_reason"] == "Missing documents"

    # Verify user account is inactive
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    with engine.connect() as conn:
        is_active = conn.execute(select(User.is_active).where(User.phone_number == phone)).scalar_one()
        assert is_active is False
    engine.dispose()

    # Login must fail with 403
    login_fail = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "V3ryStr0ng!Pass"},
    )
    assert login_fail.status_code == 403

    # 3. Approve member via POST /api/v1/members/{member_id}/approve
    approve_resp = client.post(
        f"/api/v1/members/{member_id}/approve",
        headers=headers,
        json={"status": "APPROVED"},
    )
    assert approve_resp.status_code == 200, approve_resp.text
    data = approve_resp.json()["data"]
    assert data["membership_status"] == "APPROVED"
    assert data["approved_at"] is not None

    # Verify user account is now active
    with engine.connect() as conn:
        is_active = conn.execute(select(User.is_active).where(User.phone_number == phone)).scalar_one()
        assert is_active is True
    engine.dispose()

    # User can now login successfully
    login_ok = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "V3ryStr0ng!Pass"},
    )
    assert login_ok.status_code == 200, login_ok.text
    assert login_ok.json()["tokens"]["access_token"]

    _cleanup([email], [phone])


def test_change_password_flow(client):
    phone = _next_phone()
    email = _next_email()
    assert _register(client, phone, email=email)["status"] == 200
    _approve_user(phone)

    login_resp = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "V3ryStr0ng!Pass"},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["tokens"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Unauthorized without token
    unauth = client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "myNewPassword@123", "confirm_password": "myNewPassword@123"},
    )
    assert unauth.status_code == 401

    # 2. Validation error when passwords mismatch
    mismatch = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"new_password": "myNewPassword@123", "confirm_password": "mismatchPassword@123"},
    )
    assert mismatch.status_code == 400
    assert "passwords do not match" in mismatch.json()["message"].lower()

    # 3. Validation error when password < 6 chars
    too_short = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"new_password": "12345", "confirm_password": "12345"},
    )
    assert too_short.status_code == 400

    # 4. Success using new_password + confirm_password
    success_resp = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"new_password": "myNewPassword@123", "confirm_password": "myNewPassword@123"},
    )
    assert success_resp.status_code == 200, success_resp.text
    assert success_resp.json() == {
        "success": True,
        "message": "Password updated successfully",
    }

    # Login with new password works
    new_login = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "myNewPassword@123"},
    )
    assert new_login.status_code == 200, new_login.text

    # Login with old password fails
    old_login = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "V3ryStr0ng!Pass"},
    )
    assert old_login.status_code == 401

    # 5. Fallback using "password" key instead of "new_password"
    token2 = new_login.json()["tokens"]["access_token"]
    headers2 = {"Authorization": f"Bearer {token2}"}
    fallback_resp = client.post(
        "/api/v1/auth/change-password",
        headers=headers2,
        json={"password": "anotherPassword#999", "confirm_password": "anotherPassword#999"},
    )
    assert fallback_resp.status_code == 200, fallback_resp.text
    assert fallback_resp.json()["success"] is True

    # Login with the fallback password works
    fallback_login = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": "anotherPassword#999"},
    )
    assert fallback_login.status_code == 200

    _cleanup([email], [phone])
