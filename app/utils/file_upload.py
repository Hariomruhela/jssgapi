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
