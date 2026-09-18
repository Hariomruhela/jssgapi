from __future__ import annotations

import re
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID


def to_utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def hash_refresh_token(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def is_valid_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def financial_year_for(date_value: datetime | None = None) -> str:
    dt = date_value or datetime.now(UTC)
    if dt.month >= 4:
        return f"{dt.year}-{dt.year + 1}"
    return f"{dt.year - 1}-{dt.year}"


def sanitize_search_query(query: str) -> str:
    cleaned = re.sub(r"[^\w\s@.-]", "", query).strip()
    return cleaned[:200]


def mask_email(email: str) -> str:
    try:
        local, domain = email.split("@")
        masked = local[0] + ("*" * max(len(local) - 1, 1))
        return f"{masked}@{domain}"
    except ValueError:
        return email
