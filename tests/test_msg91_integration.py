from __future__ import annotations

from typing import Any

import pytest

import app.integrations.msg91 as msg91
from app.core.exceptions import BadRequestException, UnauthorizedException


@pytest.fixture(autouse=True)
def _ensure_widget_configured(monkeypatch):
    monkeypatch.setattr(msg91.settings, "msg91_widget_id", "generated-widget-id")
    monkeypatch.setattr(msg91.settings, "msg91_widget_token", "generated-token")


def _mock_client(monkeypatch, status_code: int, body: Any) -> dict[str, Any]:
    called: dict[str, Any] = {}

    class FakeResponse:
        def __init__(self):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self._kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None, params=None):
            called["method"] = "post"
            called["url"] = url
            called["payload"] = json
            called["headers"] = headers
            called["params"] = params
            return FakeResponse()

        async def request(self, method, url, headers=None, params=None, json=None):
            called["method"] = method
            called["url"] = url
            called["payload"] = json
            called["headers"] = headers
            called["params"] = params
            return FakeResponse()

    monkeypatch.setattr(msg91.httpx, "AsyncClient", FakeAsyncClient)
    return called


def _enable_direct_mode(monkeypatch) -> None:
    monkeypatch.setattr(msg91.settings, "msg91_auth_key", "secret-auth-key")
    monkeypatch.setattr(
        msg91.settings, "msg91_otp_template_id", "tpl-12345"
    )


async def test_direct_mode_flag(monkeypatch):
    assert msg91.direct_mode() is False
    _enable_direct_mode(monkeypatch)
    assert msg91.direct_mode() is True


async def test_direct_send_otp_calls_otp_api(monkeypatch):
    _enable_direct_mode(monkeypatch)
    called = _mock_client(
        monkeypatch, 200, {"type": "success", "message": "65746865616368"}
    )
    req_id = await msg91.send_otp("+919876543210")
    assert called["method"] == "POST"
    assert called["url"] == msg91.settings.msg91_otp_api_url
    assert called["params"] == {
        "template_id": "tpl-12345",
        "mobile": "919876543210",
        "authkey": "secret-auth-key",
    }
    assert req_id == "65746865616368"


async def test_direct_send_otp_raises_bad_request_on_error(monkeypatch):
    _enable_direct_mode(monkeypatch)
    _mock_client(
        monkeypatch,
        200,
        {"type": "error", "message": "Template not approved"},
    )
    with pytest.raises(BadRequestException):
        await msg91.send_otp("+919876543210")


async def test_direct_verify_otp_returns_verified_mobile(monkeypatch):
    _enable_direct_mode(monkeypatch)
    called = _mock_client(
        monkeypatch,
        200,
        {"type": "success", "message": "verified_successfully"},
    )
    verified = await msg91.verify_otp("reqid-ignored", "123456", mobile="+919876543210")
    assert verified == "+919876543210"
    assert called["method"] == "GET"
    assert called["url"] == msg91.settings.msg91_otp_verify_url
    assert called["params"] == {
        "mobile": "919876543210",
        "otp": "123456",
        "authkey": "secret-auth-key",
    }


async def test_direct_verify_otp_invalid_raises_unauthorized(monkeypatch):
    _enable_direct_mode(monkeypatch)
    _mock_client(
        monkeypatch,
        401,
        {"type": "error", "message": "invalid_otp"},
    )
    with pytest.raises(UnauthorizedException):
        await msg91.verify_otp("reqid", "000000", mobile="+919876543210")


async def test_send_otp_reads_req_id_from_message(monkeypatch):
    called = _mock_client(
        monkeypatch,
        200,
        {"type": "success", "message": "reqid-123"},
    )
    req_id = await msg91.send_otp("+919876543210")
    assert req_id == "reqid-123"
    assert called["payload"]["widgetId"] == "generated-widget-id"
    assert called["payload"]["tokenAuth"] == "generated-token"
    assert called["payload"]["identifier"] == "919876543210"


async def test_send_otp_reads_req_id_from_data(monkeypatch):
    _mock_client(monkeypatch, 200, {"type": "success", "data": {"reqId": "req-data-1"}})
    assert await msg91.send_otp("+919876543210") == "req-data-1"


async def test_verify_otp_reads_token_from_message(monkeypatch):
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"
    _mock_client(monkeypatch, 200, {"type": "success", "message": jwt})
    token = await msg91.verify_otp("reqid-123", "123456")
    assert token == jwt


async def test_verify_otp_reads_token_from_data(monkeypatch):
    _mock_client(
        monkeypatch,
        200,
        {"type": "success", "data": {"accessToken": "data-token"}},
    )
    assert await msg91.verify_otp("reqid-123", "123456") == "data-token"


async def test_verify_access_token_reads_mobile_from_message(monkeypatch):
    _mock_client(monkeypatch, 200, {"type": "success", "message": "919876543210"})
    assert await msg91.verify_access_token("token") == {"mobile": "919876543210"}


async def test_verify_access_token_reads_mobile_from_data(monkeypatch):
    _mock_client(
        monkeypatch,
        200,
        {"type": "success", "data": {"mobile": "919876543210"}},
    )
    assert await msg91.verify_access_token("token") == {"mobile": "919876543210"}


async def test_verify_access_token_ignores_human_message(monkeypatch):
    _mock_client(
        monkeypatch,
        200,
        {"type": "success", "message": "mobile number verified successfully"},
    )
    with pytest.raises(UnauthorizedException):
        await msg91.verify_access_token("token")


async def test_send_otp_raises_bad_request_on_provider_error(monkeypatch):
    _mock_client(monkeypatch, 200, {"type": "error", "message": "invalid widget"})
    with pytest.raises(BadRequestException):
        await msg91.send_otp("+919876543210")
