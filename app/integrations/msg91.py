from __future__ import annotations

import re
from typing import Any

import httpx
import structlog

from app.config import get_settings
from app.core.exceptions import BadRequestException, UnauthorizedException

logger = structlog.get_logger(__name__)
settings = get_settings()


def _is_success(payload: dict[str, Any]) -> bool:
    type_value = str(payload.get("type", "")).lower()
    if type_value == "success":
        return True
    status = str(payload.get("status", "")).lower()
    if status in ("success", "verified", "true"):
        return True
    if payload.get("verified") is True:
        return True
    message = str(payload.get("message", "")).lower()
    return "success" in message or "verified" in message


def _pick(obj: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def _message_value(payload: Any) -> str | None:
    """Return MSG91's ``message`` value only when it is a bare token.

    The widget endpoints (``sendOtpMobile``/``verifyOtp``) return the reqId
    / JWT access token inside the ``message`` field. Human-facing messages
    contain whitespace and are deliberately ignored.
    """
    if not isinstance(payload, dict):
        return None
    message = payload.get("message")
    if message is None:
        return None
    value = str(message).strip()
    if not value or any(ch.isspace() for ch in value):
        return None
    return value


def _extract_mobile(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        mobile = _pick(data, "mobile", "Mobile", "contact", "number", "phone")
        if mobile:
            return str(mobile).strip()
        extra = data.get("extra")
        if isinstance(extra, dict):
            mobile = _pick(extra, "mobile", "Mobile", "contact", "number", "phone")
            if mobile:
                return str(mobile).strip()
    mobile = _pick(payload, "mobile", "Mobile", "contact", "number", "phone")
    if mobile:
        return str(mobile).strip()
    candidate = _message_value(payload) or (
        _message_value(data) if isinstance(data, dict) else None
    )
    if candidate and re.fullmatch(r"\+?[0-9]{7,15}", candidate):
        return candidate
    return None


def _extract_req_id(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        req_id = _pick(data, "reqId", "req_id", "requestId", "request_id")
        if req_id:
            return str(req_id)
        token = _message_value(data)
        if token:
            return token
    req_id = _pick(payload, "reqId", "req_id", "requestId", "request_id")
    if req_id:
        return str(req_id)
    return _message_value(payload)


def _widget_configured() -> bool:
    return bool(settings.msg91_widget_id and settings.msg91_widget_token)


def _direct_configured() -> bool:
    return bool(settings.msg91_auth_key and settings.msg91_otp_template_id)


def direct_mode() -> bool:
    """True when the standard OTP API is used instead of the widget.

    The widget endpoints carry CAPTCHA/anti-bot protection (``IPBlocked``)
    that a server-side integration cannot answer; the standard OTP API
    (``/api/v5/otp`` + authkey + template id) has no such guard.
    """
    return _direct_configured()


def _direct_headers() -> dict[str, str]:
    return {
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": "jssg-api/1.0",
    }


async def _request_direct(
    method: str, url: str, params: dict[str, str]
) -> httpx.Response:
    try:
        async with httpx.AsyncClient(
            timeout=settings.msg91_timeout_seconds, follow_redirects=True
        ) as client:
            return await client.request(
                method, url, params=params, headers=_direct_headers()
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "msg91_request_failed", provider="msg91", error_type="network"
        )
        raise BadRequestException(
            "Could not reach the SMS provider. Please try again."
        ) from exc


def _widget_headers() -> dict[str, str]:
    return {
        "content-type": "application/json",
        "accept": "application/json",
        "tokenAuth": settings.msg91_widget_token,
        "user-agent": "jssg-api/1.0",
    }


def _widget_body(**fields: Any) -> dict[str, Any]:
    return {
        "widgetId": settings.msg91_widget_id,
        "tokenAuth": settings.msg91_widget_token,
        **fields,
    }


def _extract_access_token(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        token = _pick(
            data,
            "accessToken",
            "access_token",
            "token",
            "tokenAuth",
        )
        if token:
            return str(token)
        message_token = _message_value(data)
        if message_token:
            return message_token
    token = _pick(
        payload,
        "accessToken",
        "access_token",
        "token",
        "access-token",
        "tokenAuth",
    )
    if token:
        return str(token)
    return _message_value(payload)


async def _post_widget(url: str, payload: dict[str, Any]) -> httpx.Response:
    try:
        async with httpx.AsyncClient(
            timeout=settings.msg91_timeout_seconds, follow_redirects=True
        ) as client:
            return await client.post(
                url, json=payload, headers=_widget_headers()
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "msg91_request_failed", provider="msg91", error_type="network"
        )
        raise BadRequestException(
            "Could not reach the SMS provider. Please try again."
        ) from exc


def _blocked_hint(message: str) -> str | None:
    lowered = (message or "").lower()
    if "ipblocked" in lowered or "ip blocked" in lowered:
        return (
            "MSG91 blocked this request (IPBlocked). This usually means the "
            "OTP Widget has CAPTCHA/anti-bot validation enabled, which a "
            "server-side integration cannot answer. Disable CAPTCHA "
            "validation on the widget in the MSG91 dashboard."
        )
    if "captcha" in lowered:
        return (
            "MSG91 rejected the OTP because the OTP Widget has CAPTCHA "
            "enabled. Disable CAPTCHA validation on the widget in the MSG91 "
            "dashboard."
        )
    return None


async def send_otp(mobile: str) -> str:
    """Ask MSG91 to send an OTP to the given mobile (international format).

    Returns the MSG91 ``reqId`` needed to later verify the OTP. No OTP is
    generated or stored locally. Raises ``BadRequestException`` on failure
    (never logs the OTP).
    """
    if direct_mode():
        return await _send_otp_direct(mobile)

    if not _widget_configured():
        raise BadRequestException(
            "Phone verification is not configured on the server"
        )

    payload = _widget_body(identifier=mobile.lstrip("+"))
    response = await _post_widget(settings.msg91_send_otp_url, payload)

    body = _safe_json(response)
    if (
        response.status_code >= 400
        or not isinstance(body, dict)
        or not _is_success(body)
    ):
        message = _safe_message(body)
        _log_response_status(response.status_code, message)
        _log_response_shape(body)
        hint = _blocked_hint(message)
        raise BadRequestException(
            _provider_message("send", response.status_code, message)
            + (f" {hint}" if hint else ""),
        )

    req_id = _extract_req_id(body)
    if not req_id:
        logger.warning("msg91_send_missing_reqid", provider="msg91")
        _log_response_shape(body)
        raise BadRequestException(
            "MSG91 accepted the request but returned no request ID. "
            "Please try again or check the widget configuration."
        )

    logger.info(
        "msg91_otp_sent", provider="msg91", mobile=_mask_mobile(mobile)
    )
    return req_id


async def _send_otp_direct(mobile: str) -> str:
    """Send an OTP through the standard MSG91 OTP API.

    Uses ``authkey`` + ``template_id`` (no widget → no CAPTCHA/anti-bot
    guard). The success body's ``message`` field is the OTP request id.
    """
    digits = mobile.lstrip("+")
    params = {
        "template_id": settings.msg91_otp_template_id or "",
        "mobile": digits,
        "authkey": settings.msg91_auth_key,
    }
    response = await _request_direct(
        "POST", settings.msg91_otp_api_url, params
    )

    body = _safe_json(response)
    if (
        response.status_code >= 400
        or not isinstance(body, dict)
        or not _is_success(body)
    ):
        message = _safe_message(body)
        _log_response_status(response.status_code, message)
        _log_response_shape(body)
        raise BadRequestException(
            _provider_message("send", response.status_code, message),
        )

    req_id = _extract_req_id(body)
    if not req_id:
        logger.warning("msg91_send_missing_reqid", provider="msg91")
        _log_response_shape(body)
        raise BadRequestException(
            "MSG91 accepted the request but returned no request ID. "
            "Please try again or check the OTP template configuration."
        )

    logger.info(
        "msg91_otp_sent",
        provider="msg91",
        mode="direct",
        mobile=_mask_mobile(mobile),
    )
    return req_id


async def verify_otp(req_id: str, otp: str, mobile: str | None = None) -> str:
    """Ask MSG91 to verify an OTP against its own records.

    In direct mode returns the verified mobile. In widget mode returns a JWT
    access token that must then be validated with ``verify_access_token``.
    Raises ``UnauthorizedException`` for invalid/expired OTP and
    ``BadRequestException`` for provider failures. Never logs the OTP or the
    returned token.
    """
    if direct_mode():
        if not mobile:
            raise BadRequestException(
                "Phone verification is not configured on the server"
            )
        return await _verify_otp_direct(mobile, otp)
    return await _verify_otp_widget(req_id, otp)


async def _verify_otp_direct(mobile: str, otp: str) -> str:
    """Verify an OTP via the standard OTP API (mobile + authkey).

    Return the verified mobile number in international format.
    """
    if not otp:
        raise UnauthorizedException("Invalid or expired OTP")

    params = {
        "mobile": mobile.lstrip("+"),
        "otp": otp.strip(),
        "authkey": settings.msg91_auth_key,
    }
    response = await _request_direct(
        "GET", settings.msg91_otp_verify_url, params
    )

    body = _safe_json(response)
    if (
        response.status_code >= 400
        or not isinstance(body, dict)
        or not _is_success(body)
    ):
        message = _safe_message(body)
        _log_response_status(response.status_code, message)
        _log_response_shape(body)
        raise UnauthorizedException(
            "Invalid OTP. Please check the OTP and try again."
        )

    logger.info(
        "msg91_otp_verified",
        provider="msg91",
        mode="direct",
        mobile=_mask_mobile(mobile),
    )
    return mobile


async def _verify_otp_widget(req_id: str, otp: str) -> str:
    if not _widget_configured():
        raise BadRequestException(
            "Phone verification is not configured on the server"
        )
    if not req_id or not otp:
        raise UnauthorizedException("Invalid or expired OTP")

    payload = _widget_body(reqId=req_id, otp=otp)
    response = await _post_widget(settings.msg91_verify_otp_url, payload)

    body = _safe_json(response)
    if (
        response.status_code >= 400
        or not isinstance(body, dict)
        or not _is_success(body)
    ):
        message = _safe_message(body)
        _log_response_status(response.status_code, message)
        _log_response_shape(body)
        raise UnauthorizedException(
            "Invalid OTP. Please check the OTP and try again."
        )

    access_token = _extract_access_token(body)
    if not access_token:
        logger.warning("msg91_verify_otp_missing_token", provider="msg91")
        _log_response_shape(body)
        raise UnauthorizedException(
            "Invalid OTP. Please check the OTP and try again."
        )

    logger.info("msg91_otp_verified", provider="msg91")
    return access_token


async def verify_access_token(access_token: str) -> dict[str, Any]:
    """Validate an OTP Widget access token against MSG91.

    Returns a dict containing the verified mobile as ``{"mobile": "+91..."}``.
    Raises ``UnauthorizedException`` for invalid/expired tokens and
    ``BadRequestException`` for provider/config/network failures.
    """
    if not _widget_configured():
        raise BadRequestException(
            "Phone verification is not configured on the server"
        )
    if not access_token:
        raise UnauthorizedException("Invalid or expired verification token")

    payload = _widget_body(accessToken=access_token)
    response = await _post_widget(settings.msg91_verify_url, payload)

    body = _safe_json(response)
    if (
        response.status_code >= 400
        or not isinstance(body, dict)
        or not _is_success(body)
    ):
        message = _safe_message(body)
        _log_response_status(response.status_code, message)
        _log_response_shape(body)
        raise UnauthorizedException(
            "Mobile number verification failed. Please verify your OTP again."
        )

    mobile = _extract_mobile(body)
    if not mobile:
        logger.warning("msg91_verify_access_token_missing_mobile", provider="msg91")
        _log_response_shape(body)
        raise UnauthorizedException(
            "Mobile number verification failed. Please verify your OTP again."
        )

    logger.info(
        "msg91_access_token_verified",
        provider="msg91",
        mobile=_mask_mobile(mobile),
    )
    return {"mobile": mobile}


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        logger.warning("msg91_invalid_response", provider="msg91")
        return None


def _log_response_shape(body: Any) -> None:
    """Log only the response structure (keys/types), never values/secrets."""
    if isinstance(body, dict):
        shape = {key: type(value).__name__ for key, value in body.items()}
    else:
        shape = {"_": type(body).__name__}
    logger.warning("msg91_response_shape", provider="msg91", response_shape=shape)


def _safe_message(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("message", ""))
    return ""


def _provider_message(action: str, status_code: int, message: str) -> str:
    details = message.strip() if message else f"HTTP {status_code}"
    return f"Could not {action} the OTP: {details}"


def _mask_mobile(mobile: str) -> str:
    digits = "".join(ch for ch in mobile if ch.isdigit())
    if len(digits) <= 4:
        return "***"
    return f"{mobile[:2]}****{mobile[-2:]}"


def _log_response_status(status_code: int, message: str = "") -> None:
    logger.warning(
        "msg91_response_failed",
        provider="msg91",
        status_code=status_code,
        message=message,
    )
