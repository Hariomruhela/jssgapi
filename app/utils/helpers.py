from __future__ import annotations

import re
from datetime import UTC, date, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID


def parse_any_date(value: Any, default_day_first: bool = True) -> date | None:
    """Parse a date value from date, datetime, or text in any standard format.

    Recognises:
    * datetime.date or datetime.datetime objects
    * ISO: YYYY-MM-DD (e.g. 1980-05-12)
    * Day-first: DD/MM/YYYY or DD-MM-YYYY (e.g. 08/05/1966, 16-10-1999)
    * Month-first (when second part > 12): MM/DD/YYYY (e.g. 11/17/1963)
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().split("T")[0].split(" ")[0]
    if not text:
        return None
    parts = text.replace(".", "-").replace("/", "-").split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    p0, p1, p2 = (int(p) for p in parts)
    if len(parts[0]) == 4:  # YYYY-MM-DD
        y, m, d = p0, p1, p2
    elif len(parts[2]) in (2, 4):
        y = p2 if len(parts[2]) == 4 else (2000 + p2 if p2 < 30 else 1900 + p2)
        if p0 > 12:  # Day > 12 -> DD/MM
            d, m = p0, p1
        elif p1 > 12:  # Month > 12 (impossible as month) -> MM/DD
            m, d = p0, p1
        elif not default_day_first:  # Month-first when explicitly requested (e.g. Google Sheet import)
            m, d = p0, p1
        else:  # Standard day-first (DD/MM/YYYY)
            d, m = p0, p1
    else:
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def format_to_dd_mm_yyyy(value: Any, default_day_first: bool = True) -> str | None:
    """Standardise any date representation into 'dd/mm/yyyy'.

    If the value cannot be parsed as a date, it is returned as-is (e.g. free-text notes).
    """
    if value is None:
        return None
    parsed = parse_any_date(value, default_day_first=default_day_first)
    if parsed is not None:
        return parsed.strftime("%d/%m/%Y")
    text = str(value).strip()
    return text if text else None


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
    cleaned = re.sub(r"[^\w\s@.+-]", "", query).strip()
    return cleaned[:200]


def mask_email(email: str) -> str:
    try:
        local, domain = email.split("@")
        masked = local[0] + ("*" * max(len(local) - 1, 1))
        return f"{masked}@{domain}"
    except ValueError:
        return email


def split_full_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split()
    if not parts:
        return full_name, ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]

