"""Pluggable object storage for uploaded media.

The upload path must never hand back a URL that nothing serves. Previously an
unconfigured environment returned ``/dev-media/<key>`` - a relative path with no
route behind it - so the API answered 200 with a ``photo_link`` that could never
render, and the uploaded bytes were discarded.

Backends:

* :class:`CloudflareR2Client` - the production backend.
* :class:`DatabaseStorage` - keeps the bytes in Postgres. This is the default
  whenever R2 is absent, because it is the only fallback that actually survives
  a serverless cold start; the API streams the bytes back from
  ``/api/v1/media/{media_id}/content``.
* :class:`LocalDiskStorage` - real files on disk, for local development and
  tests.

All expose ``upload_bytes`` / ``read_bytes`` / ``delete_object`` and return an
absolute URL only when the backend genuinely serves one over HTTP.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.exceptions import ServiceUnavailableException

logger = logging.getLogger(__name__)

settings = get_settings()

# Local fallback root. Inside a container the process temp dir is writable,
# which keeps local development working without R2 credentials.
LOCAL_ROOT = Path(tempfile.gettempdir()) / "jssg-media"

# Cloudflare exposes exactly one S3 API host for R2. The public ``r2.dev``
# domain and custom public domains serve objects over plain HTTP only: a
# request signed for the API host that is sent to one of them is answered with
# ``SignatureDoesNotMatch``, which points at the credentials instead of at the
# endpoint, so the two host shapes have to be told apart by name.
R2_API_HOST_SUFFIX = ".r2.cloudflarestorage.com"

# R2 ignores the region, but the signature is computed over it and Cloudflare
# only accepts this literal value.
R2_REGION = "auto"

# Cloudflare API tokens carry the version they were issued under. An R2 secret
# access key never looks like one, and mixing the two up is a common cause of
# ``SignatureDoesNotMatch``.
_CF_API_TOKEN_PREFIXES = ("v1.0-",)


def _r2_endpoint_error(endpoint: str) -> str:
    """Explain why ``endpoint`` cannot be used as an R2 S3 endpoint.

    Returns an empty string when the endpoint is usable, otherwise a
    secret-free sentence naming the setting to fix.
    """
    if not endpoint:
        return "CLOUDFLARE_R2_ENDPOINT is not set"
    parts = urlsplit(endpoint)
    if parts.scheme not in ("https", "http"):
        return "CLOUDFLARE_R2_ENDPOINT must be an https:// URL"
    if "@" in parts.netloc:
        return "CLOUDFLARE_R2_ENDPOINT must not embed credentials in the URL"
    host = (parts.hostname or "").lower()
    if not host:
        return "CLOUDFLARE_R2_ENDPOINT has no host"
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return "CLOUDFLARE_R2_ENDPOINT points at localhost, not at R2"
    if host.endswith(".r2.dev"):
        return (
            "CLOUDFLARE_R2_ENDPOINT is the public r2.dev domain, which cannot "
            "accept signed requests; use "
            f"https://<ACCOUNT_ID>{R2_API_HOST_SUFFIX} for the S3 endpoint and "
            "CLOUDFLARE_R2_PUBLIC_URL for the public URL"
        )
    if host.endswith(".amazonaws.com"):
        return "CLOUDFLARE_R2_ENDPOINT points at AWS S3, not at R2"
    if not host.endswith(R2_API_HOST_SUFFIX):
        return (
            "CLOUDFLARE_R2_ENDPOINT must be "
            f"https://<ACCOUNT_ID>{R2_API_HOST_SUFFIX}"
        )
    if not host[: -len(R2_API_HOST_SUFFIX)]:
        return (
            "CLOUDFLARE_R2_ENDPOINT is missing the account id; use "
            f"https://<ACCOUNT_ID>{R2_API_HOST_SUFFIX}"
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return "CLOUDFLARE_R2_ENDPOINT must not contain a path, query or fragment"
    if parts.scheme == "http":
        return "CLOUDFLARE_R2_ENDPOINT must use https, not http"
    return ""


def _account_id_from_endpoint(endpoint: str) -> str:
    """The account id embedded in the endpoint host, when there is one."""
    host = (urlsplit(endpoint).hostname or "").lower() if endpoint else ""
    if host.endswith(R2_API_HOST_SUFFIX):
        return host[: -len(R2_API_HOST_SUFFIX)]
    return ""


def _r2_credential_problems(
    access_key: str, secret_key: str, endpoint: str
) -> list[str]:
    """Credentials that are the wrong *kind* of Cloudflare secret.

    ``SignatureDoesNotMatch`` only ever says "these two strings are not a pair
    R2 accepts", so the usual mistakes - a Cloudflare API token, a Global API
    key, the account id pasted into the access key slot, or residual quoting -
    are named explicitly here. Only the diagnosis is returned; the values are
    never echoed, not even truncated.
    """
    problems: list[str] = []
    account_id = _account_id_from_endpoint(endpoint)

    if access_key:
        if re.search(r"[\s\"']", access_key):
            problems.append(
                "CLOUDFLARE_R2_ACCESS_KEY still contains whitespace or quote "
                "characters, so it is not the raw key"
            )
        elif not re.fullmatch(r"[0-9a-fA-F]{32}", access_key):
            problems.append(
                "CLOUDFLARE_R2_ACCESS_KEY does not look like an R2 access key "
                "ID (expected 32 hexadecimal characters)"
            )
        elif access_key == account_id:
            problems.append(
                "CLOUDFLARE_R2_ACCESS_KEY holds the Cloudflare account ID; the "
                "R2 access key ID is a different value from the R2 token page"
            )

    if secret_key:
        if re.search(r"[\s\"']", secret_key):
            problems.append(
                "CLOUDFLARE_R2_SECRET_KEY still contains whitespace or quote "
                "characters, so it is not the raw secret"
            )
        elif secret_key.startswith(_CF_API_TOKEN_PREFIXES):
            problems.append(
                "CLOUDFLARE_R2_SECRET_KEY looks like a Cloudflare API token; "
                "R2 needs the secret access key shown next to the R2 access key"
            )
        elif len(secret_key) == 37:
            problems.append(
                "CLOUDFLARE_R2_SECRET_KEY looks like a Cloudflare Global API "
                "key; R2 needs the R2 secret access key"
            )
        elif not re.fullmatch(r"[0-9a-fA-F]{64}", secret_key):
            problems.append(
                "CLOUDFLARE_R2_SECRET_KEY does not look like an R2 secret "
                "access key (expected 64 hexadecimal characters)"
            )

    if access_key and access_key == secret_key:
        problems.append(
            "CLOUDFLARE_R2_ACCESS_KEY and CLOUDFLARE_R2_SECRET_KEY hold the "
            "same value"
        )
    return problems


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


class DatabaseStorage(StorageBackend):
    """Store the bytes in the ``media_objects`` table.

    A serverless instance's local disk is not shared with any other instance
    and is discarded on a cold start, so a file written there is gone by the
    next request. Postgres is already configured and shared, so it is the only
    fallback that keeps uploads readable. Bytes are served back through the API
    route, so this backend never claims a public URL.
    """

    available = True
    serves_public_urls = False

    _engine = None

    @classmethod
    def _get_engine(cls):
        """A short-lived sync engine, created once.

        The storage interface is synchronous (and the R2 boto3 client is too),
        so this deliberately does not borrow the app's async session.
        """
        if cls._engine is None:
            cls._engine = create_engine(
                settings.database_url.replace(
                    "postgresql+asyncpg://", "postgresql+psycopg2://"
                ),
                poolclass=NullPool,
            )
        return cls._engine

    def upload_bytes(self, object_key: str, data: bytes, mime_type: str) -> str:
        key = _validate_object_key(object_key)
        engine = self._get_engine()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO media_objects "
                    "(object_key, content, mime_type, file_size) "
                    "VALUES (:key, :content, :mime, :size) "
                    "ON CONFLICT (object_key) DO UPDATE SET "
                    "content = EXCLUDED.content, mime_type = EXCLUDED.mime_type, "
                    "file_size = EXCLUDED.file_size, updated_at = now()"
                ),
                {
                    "key": key,
                    # psycopg2 adapts bytes to bytea in binary form, so content
                    # that begins with a backslash is still safe.
                    "content": data,
                    "mime": mime_type,
                    "size": len(data),
                },
            )
        return ""

    def read_bytes(self, object_key: str) -> bytes:
        key = _validate_object_key(object_key)
        engine = self._get_engine()
        with engine.connect() as connection:
            content = connection.execute(
                text("SELECT content FROM media_objects WHERE object_key = :key"),
                {"key": key},
            ).scalar()
        if content is None:
            raise ServiceUnavailableException(
                "Media object is missing from storage", "MEDIA_NOT_FOUND"
            )
        return bytes(content)

    def delete_object(self, object_key: str) -> None:
        key = _validate_object_key(object_key)
        engine = self._get_engine()
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM media_objects WHERE object_key = :key"), {"key": key}
            )


class CloudflareR2Client(StorageBackend):
    """Thin boto3 wrapper for Cloudflare R2 (S3-compatible).

    R2 speaks the S3 API but signs only SigV4 over the literal region ``auto``,
    against the ``<ACCOUNT_ID>.r2.cloudflarestorage.com`` host, using the R2
    access key pair - not a Cloudflare API token. Every one of those is pinned
    here, and the endpoint is validated up front, so a wrong setting surfaces as
    a named configuration error instead of a signature mismatch deep in a
    request.
    """

    def __init__(self) -> None:
        self.endpoint = settings.cloudflare_r2_endpoint
        self.access_key = settings.cloudflare_r2_access_key
        self.secret_key = settings.cloudflare_r2_secret_key
        self.bucket = settings.cloudflare_r2_bucket
        self.public_url = settings.cloudflare_r2_public_url
        self.missing = [
            name
            for name, value in (
                ("CLOUDFLARE_R2_ENDPOINT", self.endpoint),
                ("CLOUDFLARE_R2_ACCESS_KEY", self.access_key),
                ("CLOUDFLARE_R2_SECRET_KEY", self.secret_key),
                ("CLOUDFLARE_R2_BUCKET", self.bucket),
            )
            if not (value or "").strip()
        ]
        # An absent endpoint is already covered by ``missing``; only describe a
        # value that is present but unusable, so the message can list every
        # variable that needs attention.
        self.configuration_error = (
            _r2_endpoint_error(self.endpoint) if self.endpoint else ""
        )
        self.available = not self.missing and not self.configuration_error
        # Only claim a public URL when a real public base is configured.
        self.serves_public_urls = bool(self.available and self.public_url)
        # One client per instance, so PUT, GET and DELETE in the same request
        # all sign with the same endpoint, credentials, region and addressing.
        self._boto3_client = None

    @property
    def unavailable_message(self) -> str:
        """Secret-free reason this client cannot talk to R2."""
        problems = list(self.missing)
        if self.configuration_error:
            problems.append(self.configuration_error)
        if not problems:
            return "Cloudflare R2 is not configured"
        return "Cloudflare R2 is not configured: " + "; ".join(problems)

    def _client(self):
        if not self.available:
            raise ServiceUnavailableException(
                self.unavailable_message, "MEDIA_STORAGE_UNAVAILABLE"
            )
        if self._boto3_client is None:
            import boto3
            from botocore.config import Config

            # An explicit Session pins the credentials and the region, so no
            # ambient AWS_* variable or ~/.aws file on the host can change how
            # the signature is computed.
            session = boto3.session.Session(
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=R2_REGION,
            )
            self._boto3_client = session.client(
                "s3",
                endpoint_url=self.endpoint,
                config=Config(
                    # R2 accepts SigV4 only.
                    signature_version="s3v4",
                    # The bucket is not a DNS label on the R2 host, so the key
                    # belongs in the path rather than in the hostname.
                    s3={"addressing_style": "path"},
                ),
            )
        return self._boto3_client

    def upload_bytes(self, object_key: str, data: bytes, mime_type: str) -> str:
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
        response = self._client().get_object(
            Bucket=self.bucket, Key=_validate_object_key(object_key)
        )
        return bytes(response["Body"].read())

    def delete_object(self, object_key: str) -> None:
        key = _validate_object_key(object_key)
        try:
            self._client().delete_object(Bucket=self.bucket, Key=key)
        except ServiceUnavailableException:
            raise
        except Exception:
            logger.warning("Could not delete R2 object %s", key, exc_info=True)


def validate_storage_configuration() -> str:
    """Report the media storage configuration at startup.

    Returns the backend that will be used. Problems are logged by variable
    name and diagnosis only - credentials are never logged, not even
    truncated. Misconfigured R2 must not stay silent until the first media
    read fails, because by then the only evidence is a signature mismatch that
    blames the credentials regardless of what is actually wrong.
    """
    r2 = CloudflareR2Client()
    for problem in _r2_credential_problems(r2.access_key, r2.secret_key, r2.endpoint):
        logger.error("Cloudflare R2: %s", problem)
    if r2.available:
        logger.info(
            "media storage: Cloudflare R2 bucket %s via %s",
            r2.bucket,
            r2.endpoint,
        )
        if not r2.serves_public_urls:
            logger.warning(
                "CLOUDFLARE_R2_PUBLIC_URL is not set: uploads will be served "
                "through this API instead of a CDN domain."
            )
        return "r2"

    if r2.missing:
        logger.info(
            "Cloudflare R2 is not configured (missing %s)", ", ".join(r2.missing)
        )
    if r2.configuration_error:
        logger.error("Cloudflare R2: %s", r2.configuration_error)

    choice = (settings.media_storage_backend or "auto").strip().lower()
    if choice == "r2":
        logger.error(
            "MEDIA_STORAGE_BACKEND=r2 but Cloudflare R2 cannot be used (%s); "
            "media uploads and reads will fail.",
            r2.configuration_error or "missing " + ", ".join(r2.missing),
        )
        return "r2"
    if choice == "local":
        logger.info("media storage: local disk at %s", LOCAL_ROOT)
        return "local"
    if r2.missing or r2.configuration_error:
        logger.warning(
            "MEDIA_STORAGE_BACKEND=auto and Cloudflare R2 is unusable, so "
            "media is stored in Postgres; fix the CLOUDFLARE_R2_* settings to "
            "use R2."
        )
    return "database"


def get_storage() -> StorageBackend:
    """Pick a storage backend.

    ``MEDIA_STORAGE_BACKEND`` decides:

    * ``auto`` (default) - R2 when it is fully configured, else Postgres.
    * ``r2`` - R2, even if the credentials are missing (calls then fail loudly
      instead of quietly writing somewhere else).
    * ``database`` - Postgres.
    * ``local`` - local disk, for development against real files.
    """
    choice = (settings.media_storage_backend or "auto").strip().lower()
    r2 = CloudflareR2Client()

    if choice == "r2" or (choice == "auto" and r2.available):
        if not r2.available:
            logger.warning(
                "MEDIA_STORAGE_BACKEND=r2 but CLOUDFLARE_R2_* is not configured; "
                "media uploads and reads will fail."
            )
        elif not r2.serves_public_urls:
            logger.warning(
                "CLOUDFLARE_R2_PUBLIC_URL is not set: uploads will be served "
                "through this API instead of a CDN domain."
            )
        return r2

    if choice == "local":
        logger.info("media storage: local disk at %s", LOCAL_ROOT)
        return LocalDiskStorage()

    logger.info(
        "media storage: Postgres (media_objects table); files are served from %s",
        "/api/v1/media/{id}/content",
    )
    return DatabaseStorage()
