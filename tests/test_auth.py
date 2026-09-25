from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.exceptions import UnauthorizedException
from app.main import app
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
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


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
    id_token: str | None = None,
) -> dict:
    body: dict[str, object] = {
        "full_name": "Auth Test User",
        "email": None if email is _MISSING else email,
        "password": password,
        "phone_number": phone.removeprefix("+91"),
        "confirm_password": password,
        "id_token": phone if id_token is None else id_token,
    }
    resp = client.post("/api/v1/auth/register", json=body)
    return {"status": resp.status_code, "body": resp.json()}


def _cleanup(emails: list[str], phones: list[str]) -> None:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            conn.execute(delete(User).where(User.email.in_(emails)))
            conn.execute(delete(User).where(User.phone_number.in_(phones)))
            conn.commit()
    finally:
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
    assert body["tokens"]["access_token"]
    assert body["tokens"]["token_type"] == "bearer"
    assert "password_hash" not in body["user"]
    _cleanup([email], [phone])


def test_register_without_email_succeeds(client):
    phone = _next_phone()
    result = _register(client, phone)
    assert result["status"] == 200, result["body"]
    body = result["body"]
    assert body["user"]["email"] is None
    assert body["user"]["phone_number"] == phone
    assert body["tokens"]["access_token"]
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
        "message": "An account with this phone number already exists",
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


def test_register_invalid_phone_from_token(client):
    result = _register(client, "not-a-phone", id_token="not-a-phone")
    assert result["status"] == 422


def test_register_invalid_id_token(client):
    phone = _next_phone()
    result = _register(client, phone, email=_next_email(), id_token="invalid-token")
    assert result["status"] == 401
    _cleanup([], [phone])


def test_register_token_without_phone(client):
    phone = _next_phone()
    result = _register(
        client, phone, email=_next_email(), id_token="no-phone-token"
    )
    assert result["status"] == 401
    _cleanup([], [phone])


def test_register_missing_id_token_field(client):
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Auth Test User",
            "email": None,
            "password": "V3ryStr0ng!Pass",
            "phone_number": "8770948303",
            "confirm_password": "V3ryStr0ng!Pass",
        },
    )
    assert resp.status_code == 422


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
    token = result["body"]["tokens"]["access_token"]
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
