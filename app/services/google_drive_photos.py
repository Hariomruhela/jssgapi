"""Read a member photo out of Google Drive.

The member directory was populated from a Google Sheet whose photo column holds
``drive.google.com`` share links. The mobile app renders them with a plain
``Image.network`` request, which means two things:

* a link to a file that is not shared "anyone with the link" resolves to an
  ``accounts.google.com`` sign-in page, so the photo never renders;
* even a correctly shared file renders as Drive's HTML preview page, never as
  image bytes.

So a Drive link is never usable as an ``Image.network`` source. This module
fetches the bytes instead, through either of two paths, and nothing here
changes a file's sharing settings:

1. the configured service account, when the Drive scope is available to it -
   this needs the folder shared with the service account address;
2. an unauthenticated fetch, which works for files that are already shared
   "anyone with the link".

A file that is private to humans and to the service account fails with a
reason naming the fix, and is left alone.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

from app.services.google_sheets_service import (
    DRIVE_SCOPES,
    load_google_credentials,
)
from app.utils.file_upload import ALLOWED_IMAGE_TYPES, MAX_IMAGE_SIZE

logger = logging.getLogger(__name__)

# Google serves files from these hosts; a link from any of them that is not a
# recognisable file id is not something we can migrate.
DRIVE_HOSTS = frozenset(
    {
        "drive.google.com",
        "docs.google.com",
        "www.drive.google.com",
        "drive.usercontent.google.com",
    }
)

# Hosts that already serve image bytes with no sign-in. A member pointed at one
# of these renders fine and must be left exactly as it is.
DIRECT_IMAGE_HOSTS = frozenset(
    {
        "lh3.googleusercontent.com",
        "lh4.googleusercontent.com",
        "lh5.googleusercontent.com",
        "lh6.googleusercontent.com",
        "googleusercontent.com",
        "play.googleusercontent.com",
    }
)

# Every link shape the sheet has been seen to produce:
#   /file/d/<id>/view, /d/<id>/view, /open?id=<id>, /uc?export=view&id=<id>
_DRIVE_PATH_ID = re.compile(r"/(?:file/)?d/([A-Za-z0-9_-]{10,})")

# A Drive file id: url-safe base64. Used for the ``?id=`` query parameter, where
# any url-safe token of plausible length is treated as an id.
_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")

# Extension implied by the detected image type, so the object key keeps a usable
# suffix even when Drive reports no filename.
EXTENSION_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
}

# The unauthenticated endpoint Drive serves link-shared files from.
_PUBLIC_DOWNLOAD_URL = "https://drive.google.com/uc?export=download&id={file_id}"

# Anything Google serves for an inaccessible file is HTML - a sign-in page or a
# virus-scan interstitial - and must never be stored as an image.
_HTML_PREFIXES = (b"<!doctype html", b"<html", b"<?xml")

# Net calls are per-photo and run inside an admin request, so they get a hard
# deadline rather than blocking on Drive indefinitely.
DRIVE_TIMEOUT_SECONDS = 30


class DrivePhotoError(RuntimeError):
    """A photo could not be fetched. ``reason`` is safe to show an operator."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PhotoLinkKind(Enum):
    FILE_ID = "file_id"
    DIRECT_IMAGE = "direct_image"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class PhotoLink:
    kind: PhotoLinkKind
    url: str
    file_id: str | None = None


def parse_photo_link(raw_url: str | None) -> PhotoLink:
    """Classify a stored photo link.

    ``UNSUPPORTED`` covers an empty value, a local path and any non-Google URL.
    Only ``FILE_ID`` links need migrating; ``DIRECT_IMAGE`` links already render.
    """
    url = (raw_url or "").strip()
    if not url:
        return PhotoLink(PhotoLinkKind.UNSUPPORTED, url="")

    if not url.lower().startswith(("http://", "https://")):
        return PhotoLink(PhotoLinkKind.UNSUPPORTED, url=url)

    parts = urlsplit(url)
    host = parts.netloc.lower().split(":")[0]

    if host in DIRECT_IMAGE_HOSTS:
        return PhotoLink(PhotoLinkKind.DIRECT_IMAGE, url=url)

    if host not in DRIVE_HOSTS:
        return PhotoLink(PhotoLinkKind.UNSUPPORTED, url=url)

    query = parse_qs(parts.query)
    for values in (query.get("id"), query.get("docid")):
        for value in values or []:
            value = value.strip()
            path_match = _DRIVE_PATH_ID.search(value)
            if path_match:
                return PhotoLink(
                    PhotoLinkKind.FILE_ID, url=url, file_id=path_match.group(1)
                )
            if _FILE_ID.match(value):
                return PhotoLink(PhotoLinkKind.FILE_ID, url=url, file_id=value)

    match = _DRIVE_PATH_ID.search(parts.path)
    if match:
        return PhotoLink(PhotoLinkKind.FILE_ID, url=url, file_id=match.group(1))

    bare = parts.path.strip("/")
    if host == "drive.google.com" and _FILE_ID.match(bare) and "/" not in bare:
        return PhotoLink(PhotoLinkKind.FILE_ID, url=url, file_id=bare)

    return PhotoLink(PhotoLinkKind.UNSUPPORTED, url=url)


def is_drive_photo_url(raw_url: str | None) -> bool:
    return parse_photo_link(raw_url).kind is PhotoLinkKind.FILE_ID


@dataclass(frozen=True)
class DownloadedPhoto:
    content: bytes
    mime_type: str
    file_name: str


def _looks_like_html(content: bytes) -> bool:
    head = content[:512].lstrip().lower()
    return any(head.startswith(prefix) for prefix in _HTML_PREFIXES)


def _normalise_file_name(raw_name: str | None, mime_type: str, role: str) -> str:
    """Keep the original filename, but only as a safe suffix."""
    extension = EXTENSION_BY_MIME.get(mime_type, "")
    if raw_name:
        candidate = raw_name.strip()
        # Drive names are arbitrary; only the final segment and only the
        # characters that are safe in an object key or a header are kept.
        candidate = candidate.split("/")[-1].replace("\\", "").strip()
        candidate = re.sub(r"[^\w.\-]+", "_", candidate, flags=re.UNICODE)
        candidate = candidate.lstrip(".")
        if candidate and "." in candidate:
            return candidate[:120]
    return f"{role}{extension or '.img'}"


class DrivePhotoDownloader:
    """Fetches photo bytes from Drive or from a link-shared direct URL."""

    def __init__(self, settings=None, timeout: int = DRIVE_TIMEOUT_SECONDS) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.timeout = timeout
        self._service = None

    # ------------------------------------------------------------------
    # service-account path
    # ------------------------------------------------------------------
    def _drive_service(self):
        if self._service is None:
            from googleapiclient.discovery import build

            credentials = load_google_credentials(self.settings, DRIVE_SCOPES)
            self._service = build(
                "drive", "v3", credentials=credentials, cache_discovery=False
            )
        return self._service

    def _download_with_service_account(
        self, file_id: str, role: str
    ) -> DownloadedPhoto | None:
        """Download as the configured service account.

        Returns ``None`` when the service account has no access to the file, so
        the caller can still try the public path.
        """
        try:
            metadata = (
                self._drive_service()
                .files()
                .get(
                    fileId=file_id,
                    fields="id,name,mimeType,size",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except Exception as exc:
            if _is_access_error(exc):
                logger.info(
                    "drive file %s is not readable by the service account: %s",
                    file_id,
                    _reason_from(exc),
                )
                return None
            raise DrivePhotoError(_access_denied_reason(file_id, exc)) from exc

        mime_type = str(metadata.get("mimeType") or "")
        if mime_type not in ALLOWED_IMAGE_TYPES:
            raise DrivePhotoError(
                f"drive file {file_id} is {mime_type or 'of unknown type'}, "
                "not a supported image"
            )

        from googleapiclient.http import MediaIoBaseDownload

        # The declared size is checked before the body is pulled so an oversized
        # file cannot be buffered into memory.
        declared = metadata.get("size")
        if declared and int(declared) > MAX_IMAGE_SIZE:
            raise DrivePhotoError(
                f"drive file {file_id} is {int(declared)} bytes, over the "
                f"{MAX_IMAGE_SIZE} byte image limit"
            )

        request = (
            self._drive_service()
            .files()
            .get_media(fileId=file_id, supportsAllDrives=True)
        )
        buffer = BytesIO()
        try:
            downloader = MediaIoBaseDownload(buffer, request, chunksize=64 * 1024)
            done = False
            while not done:
                _status, done = downloader.next_chunk(num_retries=2)
                if buffer.tell() > MAX_IMAGE_SIZE:
                    raise DrivePhotoError(
                        f"drive file {file_id} is larger than the "
                        f"{MAX_IMAGE_SIZE} byte image limit"
                    )
        except DrivePhotoError:
            raise
        except Exception as exc:
            raise DrivePhotoError(_access_denied_reason(file_id, exc)) from exc

        content = buffer.getvalue()
        _reject_html(file_id, content)
        return DownloadedPhoto(
            content=content,
            mime_type=mime_type,
            file_name=_normalise_file_name(metadata.get("name"), mime_type, role),
        )

    # ------------------------------------------------------------------
    # public-link path
    # ------------------------------------------------------------------
    def _download_publicly(
        self, file_id: str, direct_url: str | None, role: str
    ) -> DownloadedPhoto:
        import httpx

        url = (
            direct_url
            if direct_url and is_direct_image_url(direct_url)
            else (_PUBLIC_DOWNLOAD_URL.format(file_id=file_id))
        )
        try:
            response = httpx.get(
                url,
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": "jssgapi/1.0 (photo migration)"},
            )
        except httpx.HTTPError as exc:
            raise DrivePhotoError(f"fetching {file_id} failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise DrivePhotoError(
                f"drive file {file_id} is not shared publicly and the service "
                "account cannot read it either"
            )
        if response.status_code != 200:
            raise DrivePhotoError(
                f"drive file {file_id} returned HTTP {response.status_code}"
            )

        content = response.content
        _reject_html(file_id, content)

        content_type = (response.headers.get("content-type") or "").split(";")[0]
        mime_type = content_type if content_type in ALLOWED_IMAGE_TYPES else ""
        if not mime_type:
            mime_type = _sniff_mime(content)
        if mime_type not in ALLOWED_IMAGE_TYPES:
            raise DrivePhotoError(
                f"drive file {file_id} served {content_type or 'no'} content, "
                "which is not a supported image"
            )
        if len(content) > MAX_IMAGE_SIZE:
            raise DrivePhotoError(
                f"drive file {file_id} is {len(content)} bytes, over the "
                f"{MAX_IMAGE_SIZE} byte image limit"
            )
        return DownloadedPhoto(
            content=content,
            mime_type=mime_type,
            file_name=_normalise_file_name(_file_name_from_url(url), mime_type, role),
        )

    # ------------------------------------------------------------------
    def download(self, link: PhotoLink, role: str) -> DownloadedPhoto:
        """Fetch the photo for ``link``.

        ``role`` names the slot being filled (``member`` / ``spouse``) and is
        only used to name the object when Drive reports no usable filename.
        """
        if link.kind is PhotoLinkKind.DIRECT_IMAGE:
            # Already a bytes URL, so there is nothing to resolve through Drive.
            return self._download_publicly("", link.url, role)

        if link.kind is not PhotoLinkKind.FILE_ID or not link.file_id:
            raise DrivePhotoError("not a Google Drive file link")

        photo = self._download_with_service_account(link.file_id, role)
        if photo is None:
            # The service account has no access, so try an unauthenticated
            # fetch, which works for files shared "anyone with the link".
            photo = self._download_publicly(link.file_id, None, role)
        return photo


def is_direct_image_url(raw_url: str | None) -> bool:
    return parse_photo_link(raw_url).kind is PhotoLinkKind.DIRECT_IMAGE


def _file_name_from_url(url: str) -> str | None:
    path = urlsplit(url).path
    return path.rsplit("/", 1)[-1] or None


def _sniff_mime(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image/heic"
    return ""


def _reject_html(file_id: str, content: bytes) -> None:
    if _looks_like_html(content):
        raise DrivePhotoError(
            f"drive file {file_id} served a sign-in or preview HTML page, "
            "not image bytes"
        )
    if not content:
        raise DrivePhotoError(f"drive file {file_id} is empty")


def _is_access_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "notfound",
            "not found",
            "insufficient",
            "insufficientpermissions",
            "file not found",
            "404",
            "forbidden",
            "403",
        )
    )


def _access_denied_reason(file_id: str, exc: Exception) -> str:
    return f"drive file {file_id} could not be read: {_reason_from(exc)}"


def _reason_from(exc: Exception) -> str:
    """A short, secret-free description of a Google API error."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    reason = getattr(exc, "reason", None)
    if reason:
        return str(reason)[:200]
    text = str(exc).strip()
    if not text:
        return f"HTTP {status}" if status else "unknown error"
    return text[:200]
