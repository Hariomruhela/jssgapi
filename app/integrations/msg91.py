from __future__ import annotations

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
    return str(mobile).strip() if mobile else None


def _extract_req_id(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        req_id = _pick(data, "reqId", "req_id", "requestId", "request_id")
        if req_id:
            return str(req_id)
    req_id = _pick(payload, "reqId", "req_id", "requestId", "request_id")
    return str(req_id) if req_id else None


def _extract_access_token(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        token = _pick(
            data,
            "accessToken",
            "access_token",
            "token",
            "accessToken",
            "tokenAuth",
        )
        if token:
            return str(token)
    token = _pick(
        payload,
        "accessToken",
        "access_token",
        "token",
        "access-token",
        "tokenAuth",
    )
    return str(token) if token else None


async def send_otp(mobile: str) -> str:
    """Ask MSG91 to send an OTP to the given mobile (international format).

    Returns the MSG91 ``reqId`` needed to later verify the OTP. No OTP is
    generated or stored locally. Raises ``BadRequestException`` on failure
    (never logs the OTP).
    """
    if not settings.msg91_auth_key or not settings.msg91_widget_id:
        raise BadRequestException(
            "Phone verification is not configured on the server"
        )

    headers = {"authkey": settings.msg91_auth_key, "content-type": "application/json"}
    payload = {"widgetId": settings.msg91_widget_id, "identifier": mobile}

    try:
        async with httpx.AsyncClient(
            timeout=settings.msg91_timeout_seconds, follow_redirects=False
        ) as client:
            response = await client.post(
                settings.msg91_send_otp_url, json=payload, headers=headers
            )
    except httpx.HTTPError:
        logger.warning(
            "msg91_send_failed", provider="msg91", error_type="network"
        )
        raise BadRequestException(
            "Could not send the OTP. Please try again."
        ) from None

    body = _safe_json(response)
    if (
        response.status_code >= 400
        or not isinstance(body, dict)
        or not _is_success(body)
    ):
        _log_response_status(response.status_code, _safe_message(body))
        raise BadRequestException("Could not send the OTP. Please try again.")

    req_id = _extract_req_id(body)
    if not req_id:
        logger.warning("msg91_send_missing_reqid", provider="msg91")
        raise BadRequestException("Could not send the OTP. Please try again.")

    logger.info(
        "msg91_otp_sent", provider="msg91", mobile=_mask_mobile(mobile)
    )
    return req_id


async def verify_otp(req_id: str, otp: str) -> str:
    """Ask MSG91 to verify an OTP against its own records.

    On success MSG91 returns a JWT access token that must then be validated
    with ``verify_access_token``. Raises ``UnauthorizedException`` for
    invalid/expired OTP and ``BadRequestException`` for provider failures.
    Never logs the OTP or the returned token.
    """
    if not settings.msg91_auth_key:
        raise BadRequestException(
            "Phone verification is not configured on the server"
        )
    if not req_id or not otp:
        raise UnauthorizedException("Invalid or expired OTP")

    headers = {"authkey": settings.msg91_auth_key, "content-type": "application/json"}
    payload = {"reqId": req_id, "otp": otp}

    try:
        async with httpx.AsyncClient(
            timeout=settings.msg91_timeout_seconds, follow_redirects=False
        ) as client:
            response = await client.post(
                settings.msg91_verify_otp_url, json=payload, headers=headers
            )
    except httpx.HTTPError:
        logger.warning(
            "msg91_verify_otp_network_failed", provider="msg91", error_type="network"
        )
        raise BadRequestException(
            "Could not verify the OTP. Please try again."
        ) from None

    body = _safe_json(response)
    if (
        response.status_code >= 400
        or not isinstance(body, dict)
        or not _is_success(body)
    ):
        _log_response_status(response.status_code, _safe_message(body))
        raise UnauthorizedException(
            "Invalid OTP. Please check the OTP and try again."
        )

    access_token = _extract_access_token(body)
    if not access_token:
        logger.warning("msg91_verify_otp_missing_token", provider="msg91")
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
    if not settings.msg91_auth_key:
        raise BadRequestException(
            "Phone verification is not configured on the server"
        )
    if not access_token:
        raise UnauthorizedException("Invalid or expired verification token")

    headers = {
        "authkey": settings.msg91_auth_key,
        "content-type": "application/json",
    }
    payload = {"accessToken": access_token}

    try:
        async with httpx.AsyncClient(
            timeout=settings.msg91_timeout_seconds, follow_redirects=False
        ) as client:
            response = await client.post(
                settings.msg91_verify_url, json=payload, headers=headers
            )
    except httpx.HTTPError:
        logger.warning(
            "msg91_verify_request_failed",
            provider="msg91",
            error_type="network",
        )
        raise BadRequestException(
            "Could not verify your mobile number. Please try again."
        ) from None

    body = _safe_json(response)
    if (
        response.status_code >= 400
        or not isinstance(body, dict)
        or not _is_success(body)
    ):
        _log_response_status(response.status_code, _safe_message(body))
        raise UnauthorizedException(
            "Mobile number verification failed. Please verify your OTP again."
        )

    mobile = _extract_mobile(body)
    if not mobile:
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


def _safe_message(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("message", ""))
    return ""


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
