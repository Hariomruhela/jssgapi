from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
from jose import JWTError, jwt

from app.config import get_settings

settings = get_settings()


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"), hashed_password.encode("utf-8")
        )
    except ValueError:
        return False


def create_access_token(
    subject: str,
    extra_claims: dict | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    now = datetime.now(UTC)
    expire = now + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    payload = {
        "sub": subject,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": expire,
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(subject: str) -> str:
    now = datetime.now(UTC)
    expire = now + timedelta(days=settings.refresh_token_expire_days)
    payload = {
        "sub": subject,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": expire,
        "type": "refresh",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("type") != "access":
            return None
        return payload
    except JWTError:
        return None


def decode_refresh_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("type") != "refresh":
            return None
        return payload
    except JWTError:
        return None
