from __future__ import annotations

from typing import Protocol

import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)
settings = get_settings()


class SmsProvider(Protocol):
    async def send(self, phone: str, message: str) -> None: ...


class DevSmsProvider:
    """Logs the OTP instead of sending it. Used when no provider is configured."""

    async def send(self, phone: str, message: str) -> None:
        logger.warning("dev_sms_fallback", phone=phone, message=message)


class TwilioSmsProvider:
    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number

    async def send(self, phone: str, message: str) -> None:
        import asyncio

        from twilio.rest import Client

        client = Client(self.account_sid, self.auth_token)

        def _deliver():
            client.messages.create(to=phone, from_=self.from_number, body=message)

        await asyncio.to_thread(_deliver)


def _resolve_provider() -> SmsProvider:
    provider = settings.sms_provider.strip().lower()
    if provider == "twilio":
        if not (settings.twilio_account_sid and settings.twilio_auth_token):
            raise RuntimeError(
                "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set "
                "when SMS_PROVIDER=twilio"
            )
        return TwilioSmsProvider(
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
            from_number=settings.twilio_from_number,
        )
    return DevSmsProvider()


_provider: SmsProvider | None = None


def get_sms_provider() -> SmsProvider:
    global _provider
    if _provider is None:
        _provider = _resolve_provider()
    return _provider


async def send_otp(phone: str, code: str, expiry_minutes: int) -> None:
    message = (
        f"{code} is your {settings.app_name} login OTP. "
        f"It is valid for {expiry_minutes} minutes. Do not share it with anyone."
    )
    await get_sms_provider().send(phone, message)


def is_dev_sms() -> bool:
    return settings.sms_provider.strip().lower() == ""
