from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Cloudflare R2
    cloudflare_r2_endpoint: str = ""
    cloudflare_r2_access_key: str = ""
    cloudflare_r2_secret_key: str = ""
    cloudflare_r2_bucket: str = ""
    cloudflare_r2_public_url: str = ""

    # Firebase
    firebase_project_id: str = ""
    firebase_private_key: str = ""
    firebase_client_email: str = ""

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
