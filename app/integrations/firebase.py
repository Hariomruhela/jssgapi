from __future__ import annotations

import threading
from typing import Any

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials

from app.config import FIREBASE_PROJECT_ID, get_settings
from app.core.exceptions import UnauthorizedException

_APP: firebase_admin.App | None = None
_APP_LOCK = threading.Lock()
_APP_NAME = "jssg-auth"


def firebase_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.firebase_project_id == FIREBASE_PROJECT_ID
        and settings.firebase_private_key
        and settings.firebase_client_email
    )


def _private_key_pem(value: str) -> str:
    key = value.replace("\\n", "\n").strip()
    if "-----BEGIN" in key:
        return key
    return f"-----BEGIN PRIVATE KEY-----\n{key}\n-----END PRIVATE KEY-----"


def _get_app() -> firebase_admin.App:
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
                "type": "service_account",
                "project_id": FIREBASE_PROJECT_ID,
                "private_key": _private_key_pem(settings.firebase_private_key),
                "client_email": settings.firebase_client_email,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
        try:
            app = firebase_admin.get_app(_APP_NAME)
        except ValueError:
            app = firebase_admin.initialize_app(
                cert,
                options={"projectId": FIREBASE_PROJECT_ID},
                name=_APP_NAME,
            )
        if app.project_id != FIREBASE_PROJECT_ID:
            firebase_admin.delete_app(_APP_NAME)
            app = firebase_admin.initialize_app(
                cert,
                options={"projectId": FIREBASE_PROJECT_ID},
                name=_APP_NAME,
            )
        _APP = app
        return _APP


def verify_id_token(id_token: str) -> dict[str, Any]:
    """Verify a Firebase ID token and return its claims.

    Only the Admin SDK's decoded claims are ever trusted. Any invalid,
    expired, or malformed token raises ``UnauthorizedException``.
    """
    try:
        return firebase_auth.verify_id_token(id_token, app=_get_app())
    except UnauthorizedException:
        raise
    except Exception:
        raise UnauthorizedException("Invalid or expired Firebase ID token") from None
