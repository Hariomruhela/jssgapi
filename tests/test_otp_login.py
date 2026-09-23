from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, update
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.main import app
from app.models.otp_code import OtpCode
from app.models.user import User


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _mock_firebase(monkeypatch):
    from app.core.exceptions import UnauthorizedException

    def _fake_verify_id_token(id_token: str) -> dict:
        if id_token == "invalid-token":
            raise UnauthorizedException("Invalid or expired Firebase ID token")
        return {"uid": f"uid-{id_token}", "phone_number": id_token}

    monkeypatch.setattr(
        "app.services.auth_service.firebase_verify_id_token", _fake_verify_id_token
    )


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


def _next_phone() -> str:
    suffix = str(int(time.time() * 1000))[-9:]
    return f"+916{suffix}"


def _normalized(phone: str) -> str:
    return phone.replace(" ", "").replace("-", "")


def _register(client, phone: str) -> str:
    email = f"otptest_{int(time.time() * 1000)}@test.local"
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "OTP Test User",
            "email": email,
            "password": "StrongPass123!",
            "id_token": phone,
        },
    )
    assert resp.status_code == 200, resp.text
    return email


def _request_otp(client, phone: str) -> dict:
    resp = client.post("/api/v1/auth/otp/request", json={"phone": phone})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    return body["data"]


def _cleanup(emails: list[str], phones: list[str]) -> None:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            conn.execute(delete(OtpCode).where(OtpCode.phone_number.in_(phones)))
            conn.execute(delete(User).where(User.email.in_(emails)))
            conn.commit()
    finally:
        engine.dispose()


def test_otp_request_unknown_phone_returns_404(client):
    resp = client.post(
        "/api/v1/auth/otp/request",
        json={"phone": "+916000000000"},
    )
    assert resp.status_code == 404


def test_otp_login_happy_path(client):
    phone = _next_phone()
    email = _register(client, phone)
    data = _request_otp(client, phone)
    assert data["expires_in"] == 300
    assert data["dev_otp"] is not None
    assert data["dev_otp"].isdigit() and len(data["dev_otp"]) == 6

    resp = client.post(
        "/api/v1/auth/otp/verify",
        json={"phone": phone, "otp": data["dev_otp"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["email"] == email
    assert body["tokens"]["access_token"]
    assert body["tokens"]["refresh_token"]

    verify = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {body['tokens']['access_token']}"},
    )
    assert verify.status_code == 200
    assert verify.json()["is_phone_verified"] is True
    _cleanup([email], [_normalized(phone)])


def test_otp_login_wrong_code_locks_out(client):
    phone = _next_phone()
    email = _register(client, phone)
    data = _request_otp(client, phone)
    wrong = "000000" if data["dev_otp"] != "000000" else "111111"

    for _ in range(5):
        resp = client.post(
            "/api/v1/auth/otp/verify",
            json={"phone": phone, "otp": wrong},
        )
        assert resp.status_code == 401

    resp = client.post(
        "/api/v1/auth/otp/verify",
        json={"phone": phone, "otp": data["dev_otp"]},
    )
    assert resp.status_code == 401
    _cleanup([email], [_normalized(phone)])


def test_otp_login_expired_code(client):
    phone = _next_phone()
    email = _register(client, phone)
    data = _request_otp(client, phone)

    def expire() -> None:
        engine = create_engine(_psycopg_url(), poolclass=NullPool)
        try:
            with engine.connect() as conn:
                conn.execute(
                    update(OtpCode)
                    .where(OtpCode.phone_number == _normalized(phone))
                    .values(expires_at=datetime.now(UTC) - timedelta(seconds=10))
                )
                conn.commit()
        finally:
            engine.dispose()

    expire()

    resp = client.post(
        "/api/v1/auth/otp/verify",
        json={"phone": phone, "otp": data["dev_otp"]},
    )
    assert resp.status_code == 401
    _cleanup([email], [_normalized(phone)])


def test_otp_request_rate_limited(client):
    phone = _next_phone()
    email = _register(client, phone)
    for _ in range(5):
        resp = client.post("/api/v1/auth/otp/request", json={"phone": phone})
        assert resp.status_code == 200
    resp = client.post("/api/v1/auth/otp/request", json={"phone": phone})
    assert resp.status_code == 400
    _cleanup([email], [_normalized(phone)])
