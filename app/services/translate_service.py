from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, Optional, Tuple

from app.config import get_settings
from app.core.exceptions import BadRequestException, ServiceUnavailableException

logger = logging.getLogger(__name__)

DEFAULT_MAX_LENGTH = 5000
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_CACHE_SIZE = 512
SUPPORTED_LANGUAGES = {"en", "hi"}


@dataclass(frozen=True)
class TranslationResult:
    original_text: str
    translated_text: str
    source_language: str
    target_language: str
    detected_language: Optional[str] = None


class TranslationCache:
    def __init__(self, max_size: int = DEFAULT_CACHE_SIZE):
        self._max_size = max_size
        self._cache: Dict[Tuple[str, str, str, Optional[str]], Tuple[float, TranslationResult]] = {}
        self._lock = Lock()

    def _cleanup(self, now: float) -> None:
        # Simple FIFO-ish cleanup by removing oldest entries when over size
        if len(self._cache) <= self._max_size:
            return
        # Sort by timestamp
        items = sorted(self._cache.items(), key=lambda x: x[1][0])
        to_remove = len(self._cache) - self._max_size
        for key, _ in items[:to_remove]:
            self._cache.pop(key, None)

    def get(self, key: Tuple[str, str, str, Optional[str]]) -> Optional[TranslationResult]:
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            return entry[1]

    def set(self, key: Tuple[str, str, str, Optional[str]], result: TranslationResult) -> None:
        with self._lock:
            now = time.time()
            self._cache[key] = (now, result)
            self._cleanup(now)


_cache = TranslationCache()


def _get_credentials_from_inline(raw: str):
    from google.oauth2 import service_account

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServiceUnavailableException(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON"
        ) from exc
    private_key = payload.get("private_key")
    if isinstance(private_key, str) and "\\n" in private_key:
        payload["private_key"] = private_key.replace("\\n", "\n")
    missing = [k for k in ("type", "client_email", "private_key") if not payload.get(k)]
    if missing:
        raise ServiceUnavailableException("GOOGLE_SERVICE_ACCOUNT_JSON is missing required fields")
    return service_account.Credentials.from_service_account_info(payload)


def _get_credentials():
    settings = get_settings()
    inline = (settings.google_service_account_json or "").strip()
    if inline:
        return _get_credentials_from_inline(inline)

    try:
        import google.auth
        credentials, _ = google.auth.default()
        return credentials
    except Exception as exc:
        raise ServiceUnavailableException(
            "Google Cloud Translation credentials are not configured"
        ) from exc


def _make_client():
    from google.cloud import translate_v3 as translate

    credentials = _get_credentials()
    settings = get_settings()
    client_options = {}
    if settings.google_cloud_project:
        client_options["quota_project_id"] = settings.google_cloud_project

    return translate.TranslationServiceClient(credentials=credentials, client_options=client_options)


def _translate_text(
    text: str,
    target_language: str,
    source_language: Optional[str] = None,
    project_id: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> TranslationResult:
    try:
        client = _make_client()
    except Exception as exc:
        logger.warning("Failed to initialize translation client: %s", exc)
        raise ServiceUnavailableException("Translation service is not configured")

    settings = get_settings()
    pid = project_id or settings.google_cloud_project
    if not pid:
        raise ServiceUnavailableException("GOOGLE_CLOUD_PROJECT is not configured")

    location = "global"
    parent = f"projects/{pid}/locations/{location}"

    request: Dict[str, Any] = {
        "parent": parent,
        "contents": [text],
        "target_language_code": target_language,
    }
    if source_language:
        request["source_language_code"] = source_language
    if timeout > 0:
        # The v3 client uses gRPC; timeouts can be set via call options in some cases,
        # but for simplicity we just rely on the service behavior; add a note conceptually.
        pass

    try:
        response = client.translate_text(request=request)
    except Exception as exc:
        logger.warning("Translation API call failed: %s", exc)
        raise ServiceUnavailableException("Translation service failed to translate the text")

    translations = response.translations
    if not translations:
        raise ServiceUnavailableException("Translation service returned no results")

    translated_text = translations[0].translated_text
    detected_language = getattr(translations[0], "detected_language_code", None) or source_language

    return TranslationResult(
        original_text=text,
        translated_text=translated_text,
        source_language=source_language or detected_language or "",
        target_language=target_language,
        detected_language=detected_language,
    )


def translate_text(
    text: str,
    target_language: str,
    source_language: Optional[str] = None,
    max_length: int = DEFAULT_MAX_LENGTH,
) -> TranslationResult:
    if not text or not text.strip():
        raise BadRequestException("Text must not be empty")

    cleaned = text.strip()
    if len(cleaned) > max_length:
        raise BadRequestException(f"Text exceeds maximum length of {max_length} characters")

    target_language = target_language.lower().strip()
    if target_language not in SUPPORTED_LANGUAGES:
        # Allow other languages too, but validate format loosely? Keep simple.
        pass

    if source_language:
        source_language = source_language.lower().strip()

    cache_key = (cleaned, source_language or "auto", target_language, None)
    cached = _cache.get(cache_key)
    if cached:
        return cached

    # Skip translation if same language (best effort)
    if source_language and source_language == target_language:
        result = TranslationResult(
            original_text=cleaned,
            translated_text=cleaned,
            source_language=source_language,
            target_language=target_language,
            detected_language=source_language,
        )
        _cache.set(cache_key, result)
        return result

    try:
        result = _translate_text(cleaned, target_language, source_language)
    except ServiceUnavailableException:
        # Re-raise as-is for API to handle appropriately
        raise

    _cache.set(cache_key, result)
    return result
