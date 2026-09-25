from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# libpq/psycopg2-only connection-string query params. SQLAlchemy's asyncpg
# dialect forwards every URL query param as an asyncpg.connect() keyword
# argument, and asyncpg rejects these. asyncpg uses its own ``ssl`` setting
# (default "prefer", which negotiates TLS with Neon) and handles channel
# binding itself, so these can safely be dropped.
_LIBPQ_ONLY_QUERY_PARAMS = {"sslmode", "channel_binding", "pgbouncer"}

FIREBASE_PROJECT_ID = "jssg-7e2b0"


def _async_database_url(url: str) -> str:
    """Force the SQLAlchemy async engine to use an async driver.

    Neon/Vercel often provide a plain ``postgresql://`` (or legacy
    ``postgres://`` / ``postgresql+psycopg2://``) URL. ``create_async_engine``
    maps that scheme to the default sync driver (psycopg2), which fails with
    "The asyncio extension requires an async driver". Rewrite the scheme to
    ``postgresql+asyncpg://`` unless an async driver is already selected.
    """
    url = url.strip()
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql+psycopg2://"):
        url = "postgresql+asyncpg://" + url[len("postgresql+psycopg2://") :]
    elif url.startswith("postgresql+asyncpg://"):
        pass
    else:
        return url

    parts = urlsplit(url)
    if parts.query:
        kept = [
            (key, value)
            for key, value in parse_qsl(parts.query)
            if key not in _LIBPQ_ONLY_QUERY_PARAMS
        ]
        url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
        )
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_name: str = "JSSG API"
    app_env: Literal["development", "staging", "production"] = "development"
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # Database
    database_url: str = "postgresql+asyncpg://jssg:jssg_secret@localhost:5432/jssg_db"
    database_echo: bool = False

    @field_validator("database_url", mode="before")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        return _async_database_url(value)

    # JWT
    jwt_secret: str = "CHANGE-ME-IN-PRODUCTION"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # OTP login
    otp_length: int = 6
    otp_expire_seconds: int = 300
    otp_max_attempts: int = 5
    otp_max_per_phone_per_hour: int = 5

    # SMS
    sms_provider: str = ""
    sms_api_key: str = ""
    sms_sender_id: str = ""
    sms_template_id: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # MSG91 OTP
    msg91_auth_key: str = ""
    msg91_otp_template_id: str = ""
    msg91_otp_api_url: str = "https://control.msg91.com/api/v5/otp"
    msg91_otp_verify_url: str = "https://control.msg91.com/api/v5/otp/verify"

    # MSG91 OTP Widget (fallback)
    msg91_widget_id: str = ""
    msg91_widget_token: str = ""
    msg91_verify_url: str = (
        "https://control.msg91.com/api/v5/widget/verifyAccessToken"
    )
    msg91_send_otp_url: str = (
        "https://control.msg91.com/api/v5/widget/sendOtpMobile"
    )
    msg91_verify_otp_url: str = "https://control.msg91.com/api/v5/widget/verifyOtp"
    msg91_timeout_seconds: float = 10.0

    # Cloudflare R2
    cloudflare_r2_endpoint: str = ""
    cloudflare_r2_access_key: str = ""
    cloudflare_r2_secret_key: str = ""
    cloudflare_r2_bucket: str = ""
    cloudflare_r2_public_url: str = ""

    # Firebase
    firebase_project_id: str = FIREBASE_PROJECT_ID
    firebase_private_key: str = ""
    firebase_client_email: str = ""

    @field_validator("firebase_project_id", mode="before")
    @classmethod
    def _pin_firebase_project_id(cls, value: object) -> str:
        return FIREBASE_PROJECT_ID

    # Payment
    payment_provider: str = ""
    payment_key: str = ""
    payment_secret: str = ""
    payment_webhook_secret: str = ""

    # CORS
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
