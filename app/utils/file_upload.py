from __future__ import annotations

import uuid
from pathlib import Path

from app.core.exceptions import ValidationException

ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
}
ALLOWED_VIDEO_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-matroska",
}
ALLOWED_DOCUMENT_TYPES = {
    "application/pdf",
}

ALLOWED_EXTENSIONS: dict[str, set[str]] = {
    "image": {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".heic",
    },
    "video": {".mp4", ".mov", ".webm", ".mkv"},
    "document": {".pdf"},
}

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_VIDEO_SIZE = 200 * 1024 * 1024  # 200 MB
MAX_DOCUMENT_SIZE = 25 * 1024 * 1024  # 25 MB


def validate_mime_type(mime_type: str) -> str:
    if mime_type in ALLOWED_IMAGE_TYPES:
        return "image"
    if mime_type in ALLOWED_VIDEO_TYPES:
        return "video"
    if mime_type in ALLOWED_DOCUMENT_TYPES:
        return "document"
    raise ValidationException(f"Unsupported file type: {mime_type}")


def validate_file_size(file_size: int, category: str) -> None:
    limits = {
        "image": MAX_IMAGE_SIZE,
        "video": MAX_VIDEO_SIZE,
        "document": MAX_DOCUMENT_SIZE,
    }
    limit = limits[category]
    if file_size > limit:
        max_mb = limit // (1024 * 1024)
        raise ValidationException(
            f"File size exceeds the {max_mb} MB limit for {category} files"
        )


def validate_extension(filename: str, category: str) -> None:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS[category]:
        raise ValidationException(f"Unsupported file extension '{suffix}'")


def build_object_key(category: str, filename: str, owner_id: uuid.UUID) -> str:
    suffix = Path(filename).suffix.lower() or ".bin"
    return f"{category}/{owner_id}/{uuid.uuid4().hex}{suffix}"


#: Magic numbers for the image types the API accepts, in declaration order.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

#: Brands an ISO-BMFF ``ftyp`` box may carry and still be a HEIC/HEIF image.
_HEIC_BRANDS = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"}
)


def sniff_image_type(content: bytes) -> str | None:
    """Return the image type the bytes actually are, or ``None``.

    The declared content type comes from the client and the extension comes
    from the file name, so neither can be trusted to describe the payload.
    Only the leading bytes decide what a file is.
    """
    for signature, mime_type in _IMAGE_SIGNATURES:
        if content.startswith(signature):
            return mime_type
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if len(content) >= 12 and content[4:8] == b"ftyp":
        brand = content[8:12]
        if brand in _HEIC_BRANDS or brand in {b"mif1", b"msf1"}:
            return "image/heic"
    return None
