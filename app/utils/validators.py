from __future__ import annotations

import re

from app.core.exceptions import ValidationException

EMAIL_REGEX = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PHONE_REGEX = re.compile(r"^\+?[0-9]{7,15}$")

WEAK_PASSWORD_HINTS = [
    "password",
    "123456",
    "12345678",
    "qwerty",
    "abc123",
    "jain",
    "jssg",
]


def validate_email(email: str) -> bool:
    return EMAIL_REGEX.match(email) is not None


def validate_phone(phone: str) -> bool:
    return PHONE_REGEX.match(phone) is not None


def validate_password(password: str) -> None:
    if len(password) < 8:
        raise ValidationException("Password must be at least 8 characters long")
    if len(password) > 128:
        raise ValidationException("Password must not exceed 128 characters")
    lowered = password.lower()
    if any(hint in lowered for hint in WEAK_PASSWORD_HINTS):
        raise ValidationException("Password is too weak and commonly used")
    if not any(c.islower() for c in password):
        raise ValidationException("Password must contain at least one lowercase letter")
    if not any(c.isupper() for c in password):
        raise ValidationException("Password must contain at least one uppercase letter")
    if not any(c.isdigit() for c in password):
        raise ValidationException("Password must contain at least one number")


def validate_indian_phone(phone: str) -> bool:
    cleaned = phone.replace(" ", "").replace("-", "")
    if cleaned.startswith("+91"):
        cleaned = cleaned[3:]
    return re.match(r"^[6-9][0-9]{9}$", cleaned) is not None


def normalize_phone_number(phone: str) -> str:
    """Normalize an Indian phone number to E.164 (+91XXXXXXXXXX)."""
    if not phone:
        raise ValidationException("Phone number is required")
    cleaned = phone.strip().replace(" ", "").replace("-", "")
    if cleaned.startswith("+91"):
        cleaned = cleaned[3:]
    elif cleaned.startswith("0") and len(cleaned) > 10:
        cleaned = cleaned[1:]
    if re.match(r"^[6-9][0-9]{9}$", cleaned) is None:
        raise ValidationException("Invalid Indian phone number")
    return f"+91{cleaned}"
