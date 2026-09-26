"""Pluggable object storage for uploaded media.

The upload path must never hand back a URL that nothing serves. Previously an
unconfigured environment returned ``/dev-media/<key>`` - a relative path with no
route behind it - so the API answered 200 with a ``photo_link`` that could never
render, and the uploaded bytes were discarded.

Backends:

* :class:`CloudflareR2Client` - the production backend.
* :class:`LocalDiskStorage` - writes to a real directory so local and test
  environments exercise the same read/write path as production.

Both expose ``upload_bytes`` / ``read_bytes`` / ``delete_object`` and return an
absolute URL only when the backend genuinely serves one over HTTP.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from app.config import get_settings
from app.core.exceptions import ServiceUnavailableException

logger = logging.getLogger(__name__)

settings = get_settings()

# Local fallback root. Inside a container the process temp dir is writable,
# which keeps local development working without R2 credentials.
LOCAL_ROOT = Path(tempfile.gettempdir()) / "jssg-media"


class StorageBackend(ABC):
    """Common contract for media storage."""

    #: True when the backend can actually persist objects.
    available: bool = False

    #: True when objects are served over HTTP from a public URL.
    serves_public_urls: bool = False

    @abstractmethod
    def upload_bytes(self, object_key: str, data: bytes, mime_type: str) -> str:
        """Persist the object and return an absolute URL if one exists."""

    @abstractmethod
    def read_bytes(self, object_key: str) -> bytes:
        """Return the stored bytes."""

    @abstractmethod
    def delete_object(self, object_key: str) -> None:
        """Remove the object; missing objects are not an error."""


def _validate_object_key(object_key: str) -> str:
    """Reject keys that could escape the storage root."""
    key = object_key.strip().lstrip("/")
    if not key:
        raise ServiceUnavailableException("Invalid media object key")
    parts = Path(key).parts
    if any(part in ("..", "") for part in parts) or Path(key).is_absolute():
        raise ServiceUnavailableException("Invalid media object key")
    return key


class LocalDiskStorage(StorageBackend):
    """Filesystem backend used when R2 is not configured.

    Keeps local development and tests honest: bytes really are written and
    really can be read back through the media route.
    """

    available = True
    serves_public_urls = False

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or LOCAL_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, object_key: str) -> Path:
        return self.root / _validate_object_key(object_key)

    def upload_bytes(self, object_key: str, data: bytes, mime_type: str) -> str:
        path = self._path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return ""

    def read_bytes(self, object_key: str) -> bytes:
        path = self._path(object_key)
        if not path.is_file():
            raise ServiceUnavailableException(
                "Media object is missing from storage", "MEDIA_NOT_FOUND"
            )
        return path.read_bytes()

    def delete_object(self, object_key: str) -> None:
        path = self._path(object_key)
        if path.is_file():
            path.unlink()
        # Prune now-empty parent directories up to the root.
        parent = path.parent
        while parent != self.root and self.root in parent.parents:
            if any(parent.iterdir()):
                break
            parent.rmdir()
            parent = parent.parent

    def clear(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)


class CloudflareR2Client(StorageBackend):
    """Thin boto3 wrapper for Cloudflare R2 (S3-compatible)."""

    def __init__(self) -> None:
        self.endpoint = settings.cloudflare_r2_endpoint
        self.access_key = settings.cloudflare_r2_access_key
        self.secret_key = settings.cloudflare_r2_secret_key
        self.bucket = settings.cloudflare_r2_bucket
        self.public_url = settings.cloudflare_r2_public_url
        self.available = bool(
            self.endpoint and self.access_key and self.secret_key and self.bucket
        )
        # Only claim a public URL when a real public base is configured.
        self.serves_public_urls = bool(self.available and self.public_url)

    def _client(self):
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name="auto",
        )

    def upload_bytes(self, object_key: str, data: bytes, mime_type: str) -> str:
        if not self.available:
            return ""
        key = _validate_object_key(object_key)
        self._client().put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=mime_type,
        )
        if self.serves_public_urls:
            return f"{self.public_url.rstrip('/')}/{key}"
        return ""

    def read_bytes(self, object_key: str) -> bytes:
        if not self.available:
            raise ServiceUnavailableException(
                "Cloudflare R2 is not configured", "MEDIA_STORAGE_UNAVAILABLE"
            )
        response = self._client().get_object(
            Bucket=self.bucket, Key=_validate_object_key(object_key)
        )
        return bytes(response["Body"].read())

    def delete_object(self, object_key: str) -> None:
        if not self.available:
            return
        try:
            self._client().delete_object(
                Bucket=self.bucket, Key=_validate_object_key(object_key)
            )
        except Exception:
            logger.warning("Could not delete R2 object %s", object_key, exc_info=True)


def get_storage() -> StorageBackend:
    """Return the backend to use, warning loudly when falling back to disk."""
    r2 = CloudflareR2Client()
    if r2.available:
        if not r2.serves_public_urls:
            logger.warning(
                "CLOUDFLARE_R2_PUBLIC_URL is not set: uploads will be served "
                "through this API instead of a CDN domain."
            )
        return r2
    logger.warning(
        "Cloudflare R2 is not configured; storing media on local disk at %s. "
        "Set CLOUDFLARE_R2_* for production - local disk is not shared between "
        "serverless instances and does not survive a cold start.",
        LOCAL_ROOT,
    )
    return LocalDiskStorage()
