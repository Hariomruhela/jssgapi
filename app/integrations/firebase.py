from __future__ import annotations

import threading
from typing import Any

import firebase_admin
from firebase_admin import credentials
from firebase_admin.auth import verify_id_token as _verify_id_token

from app.config import get_settings
from app.core.exceptions import UnauthorizedException

_APP: firebase_admin.App | None = None
_APP_LOCK = threading.Lock()


def firebase_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.firebase_project_id
        and settings.firebase_private_key
        and settings.firebase_client_email
    )


def _get_app() -> firebase_admin.App:
    """Return the default Firebase app, initializing it exactly once.

    Safe against double initialization (dev reload / repeated imports):
    if an app was already created in this process, reuse it.
    """
    global _APP
    if _APP is not None:
        return _APP
    if not firebase_configured():
        raise UnauthorizedException("Firebase is not configured on the server")
    with _APP_LOCK:
        if _APP is not None:
            return _APP
        settings = get_settings()
        cert = credentials.Certificate(
            {
                "project_id": settings.firebase_project_id,
                "private_key": settings.firebase_private_key,
                "client_email": settings.firebase_client_email,
            }
        )
        try:
            _APP = firebase_admin.get_app()
        except ValueError:
            _APP = firebase_admin.initialize_app(cert)
        return _APP


def verify_id_token(id_token: str) -> dict[str, Any]:
    """Verify a Firebase ID token and return its claims.

    Only the Admin SDK's decoded claims are ever trusted. Any invalid,
    expired, or malformed token raises ``UnauthorizedException``.
    """
    try:
        return _verify_id_token(id_token, app=_get_app())
    except UnauthorizedException:
        raise
    except Exception:
        raise UnauthorizedException("Invalid or expired Firebase ID token") from None
