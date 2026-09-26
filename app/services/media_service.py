from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.constants import AuditAction, MediaType
from app.core.exceptions import BadRequestException
from app.models.media import Media
from app.repositories.media_repository import MediaRepository
from app.services.audit_service import AuditService
from app.services.media_storage import StorageBackend, get_storage
from app.utils.file_upload import (
    build_object_key,
    validate_extension,
    validate_file_size,
    validate_mime_type,
)

logger = logging.getLogger(__name__)
settings = get_settings()

#: Path that actually streams the bytes, relative to the API root.
MEDIA_CONTENT_PATH = "/api/v1/media/{media_id}/content"


def build_media_url(media_id: uuid.UUID, base_url: str | None = None) -> str:
    """Absolute URL the API itself serves media from.

    ``base_url`` normally comes from the incoming request, so the URL stays
    correct on any deployment domain. Falls back to ``PUBLIC_BASE_URL`` and
    finally to a relative path.
    """
    path = MEDIA_CONTENT_PATH.format(media_id=media_id)
    base = (base_url or settings.public_base_url or "").rstrip("/")
    return f"{base}{path}" if base else path


def _is_absolute(url: str) -> bool:
    return url.startswith("https://") or url.startswith("http://")


class MediaService:
    def __init__(
        self,
        session: AsyncSession,
        storage: StorageBackend | None = None,
    ):
        self.session = session
        self.repo = MediaRepository(session)
        self.storage = storage or get_storage()
        self.audit = AuditService(session)

    async def upload(
        self,
        *,
        owner_type: str,
        owner_id: uuid.UUID,
        file_name: str,
        content: bytes,
        mime_type: str,
        width: int | None = None,
        height: int | None = None,
        is_public: bool = True,
        uploaded_by: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        base_url: str | None = None,
    ) -> Media:
        category = validate_mime_type(mime_type)
        validate_extension(file_name, category)
        validate_file_size(len(content), category)
        if len(content) == 0:
            raise BadRequestException("Uploaded file is empty")

        file_type = {
            "image": MediaType.IMAGE.value,
            "video": MediaType.VIDEO.value,
            "document": MediaType.DOCUMENT.value,
        }[category]

        object_key = build_object_key(category, file_name, owner_id)
        # Raises rather than silently dropping the bytes, so a failed upload can
        # never be reported as a success with an unusable URL.
        storage_url = self.storage.upload_bytes(object_key, content, mime_type)

        media = await self.repo.create(
            owner_type=owner_type,
            owner_id=owner_id,
            file_name=file_name,
            file_type=file_type,
            mime_type=mime_type,
            file_size=len(content),
            r2_object_key=object_key,
            url="",
            width=width,
            height=height,
            is_public=is_public,
            uploaded_by=uploaded_by,
        )

        # Prefer a real CDN/public URL when the backend has one, otherwise
        # point at the route that streams the bytes.
        media.url = (
            storage_url
            if _is_absolute(storage_url)
            else build_media_url(media.id, base_url)
        )

        await self.audit.log(
            AuditAction.MEDIA_UPLOAD,
            user_id=uploaded_by,
            entity_type=owner_type,
            entity_id=owner_id,
            details={"file_name": file_name, "file_size": len(content)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        return media

    def read(self, media: Media) -> bytes:
        return self.storage.read_bytes(media.r2_object_key)

    async def delete(
        self,
        media: Media,
        *,
        user_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.storage.delete_object(media.r2_object_key)
        await self.repo.delete(media)
        await self.audit.log(
            AuditAction.MEDIA_DELETE,
            user_id=user_id,
            entity_type=media.owner_type,
            entity_id=media.owner_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
