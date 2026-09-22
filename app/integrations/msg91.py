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


def _extract_mobile(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        mobile = _pick_mobile(data)
        if mobile:
            return mobile
        extra = data.get("extra")
        if isinstance(extra, dict):
            mobile = _pick_mobile(extra)
            if mobile:
                return mobile
    return _pick_mobile(payload)


def _pick_mobile(obj: dict[str, Any]) -> str | None:
    for key in ("mobile", "Mobile", "contact", "number", "phone"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


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
            timeout=settings.msg91_timeout_seconds,
            follow_redirects=False,
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

    if response.status_code >= 400:
        _log_response_status(response.status_code)
        raise UnauthorizedException(
            "Mobile number verification failed. Please verify your OTP again."
        )

    try:
        body = response.json()
    except ValueError:
        logger.warning("msg91_verify_invalid_response", provider="msg91")
        raise BadRequestException(
            "Could not verify your mobile number. Please try again."
        ) from None

    if not isinstance(body, dict) or not _is_success(body):
        _log_response_status(response.status_code, message=str(body.get("message", "")))
        raise UnauthorizedException(
            "Mobile number verification failed. Please verify your OTP again."
        )

    mobile = _extract_mobile(body)
    if not mobile:
        raise UnauthorizedException(
            "Mobile number verification failed. Please verify your OTP again."
        )

    logger.info(
        "msg91_token_verified",
        provider="msg91",
        mobile=_mask_mobile(mobile),
    )
    return {"mobile": mobile}


def _mask_mobile(mobile: str) -> str:
    digits = "".join(ch for ch in mobile if ch.isdigit())
    if len(digits) <= 4:
        return "***"
    return f"{mobile[:2]}****{mobile[-2:]}"


def _log_response_status(status_code: int, message: str = "") -> None:
    logger.warning(
        "msg91_verify_failed",
        provider="msg91",
        status_code=status_code,
        message=message,
    )
