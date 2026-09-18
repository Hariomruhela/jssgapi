from __future__ import annotations

from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    hash_password,
    verify_password,
)


def test_password_hashing_roundtrip():
    hashed = hash_password("V3ryStr0ng!Pass")
    assert hashed != "V3ryStr0ng!Pass"
    assert hashed.startswith("$2b$")
    assert verify_password("V3ryStr0ng!Pass", hashed) is True


def test_password_hash_is_unique_per_hashed_value():
    h1 = hash_password("SamePass123")
    h2 = hash_password("SamePass123")
    assert h1 != h2
    assert verify_password("SamePass123", h1) is True
    assert verify_password("SamePass123", h2) is True


def test_password_verification_rejects_wrong_password():
    hashed = hash_password("RightPass123")
    assert verify_password("WrongPass123", hashed) is False


def test_password_verification_rejects_malformed_hash():
    assert verify_password("Whatever123", "not-a-bcrypt-hash") is False


def test_access_token_roundtrip():
    token = create_access_token(subject="user-123")
    payload = decode_access_token(token)
    assert payload is not None
    assert payload["sub"] == "user-123"
    assert payload["type"] == "access"
    assert "exp" in payload


def test_refresh_token_roundtrip():
    token = create_refresh_token(subject="user-123")
    payload = decode_refresh_token(token)
    assert payload is not None
    assert payload["sub"] == "user-123"
    assert payload["type"] == "refresh"


def test_refresh_token_cannot_be_used_as_access_token():
    token = create_refresh_token(subject="user-123")
    assert decode_access_token(token) is None


def test_access_token_cannot_be_used_as_refresh_token():
    token = create_access_token(subject="user-123")
    assert decode_refresh_token(token) is None


def test_tampered_token_is_rejected():
    token = create_access_token(subject="user-123")
    tampered = token[:-4] + "AAAA"
    assert decode_access_token(tampered) is None
