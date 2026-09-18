from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.constants import AuditAction, MediaType
from app.core.exceptions import BadRequestException
from app.models.media import Media
from app.repositories.media_repository import MediaRepository
from app.services.audit_service import AuditService
from app.utils.file_upload import (
    build_object_key,
    validate_extension,
    validate_file_size,
    validate_mime_type,
)

settings = get_settings()


class CloudflareR2Client:
    """Thin boto3 wrapper. Falls back to dev-mode when R2 is not configured."""

    def __init__(self) -> None:
        self.endpoint = settings.cloudflare_r2_endpoint
        self.access_key = settings.cloudflare_r2_access_key
        self.secret_key = settings.cloudflare_r2_secret_key
        self.bucket = settings.cloudflare_r2_bucket
        self.public_url = settings.cloudflare_r2_public_url
        self.available = bool(
            self.endpoint and self.access_key and self.secret_key and self.bucket
        )

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
            return f"/dev-media/{object_key}"
        self._client().put_object(
            Bucket=self.bucket,
            Key=object_key,
            Body=data,
            ContentType=mime_type,
        )
        if self.public_url:
            return f"{self.public_url.rstrip('/')}/{object_key}"
        return f"/{object_key}"

    def delete_object(self, object_key: str) -> None:
        if not self.available:
            return
        try:
            self._client().delete_object(Bucket=self.bucket, Key=object_key)
        except Exception:
            pass


class MediaService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = MediaRepository(session)
        self.storage = CloudflareR2Client()
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
        url = self.storage.upload_bytes(object_key, content, mime_type)

        media = await self.repo.create(
            owner_type=owner_type,
            owner_id=owner_id,
            file_name=file_name,
            file_type=file_type,
            mime_type=mime_type,
            file_size=len(content),
            r2_object_key=object_key,
            url=url,
            width=width,
            height=height,
            is_public=is_public,
            uploaded_by=uploaded_by,
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
