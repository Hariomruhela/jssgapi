from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.core.constants import RoleName
from app.core.exceptions import ForbiddenException, NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok
from app.dependencies import (
    CurrentUser,
    DBSession,
    OptionalUser,
    require_permission,
)
from app.models.media import Media
from app.models.member import Member
from app.models.user import User
from app.repositories.media_repository import MediaRepository
from app.schemas.media import MediaOut
from app.services.media_service import MediaService

router = APIRouter(prefix="/media", tags=["Media"])

UPLOAD = Depends(require_permission(Permission.MEDIA_UPLOAD))
DELETE = Depends(require_permission(Permission.MEDIA_DELETE))


@router.get("")
async def list_media(
    db: DBSession,
    owner_type: str = Query(...),
    owner_id: UUID = Query(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = MediaRepository(db)
    items = await repo.list_for_owner(
        owner_type, owner_id, limit=page_size, offset=(page - 1) * page_size
    )
    return ok("Media fetched successfully", [MediaOut.model_validate(m) for m in items])


@router.post("/upload", dependencies=[UPLOAD])
async def upload_media(
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
    file: UploadFile = File(...),
    owner_type: str = Form(...),
    owner_id: UUID = Form(...),
    is_public: bool = Form(default=True),
):
    content = await file.read()
    service = MediaService(db)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    media = await service.upload(
        owner_type=owner_type,
        owner_id=owner_id,
        file_name=file.filename or "unnamed",
        content=content,
        mime_type=file.content_type or "application/octet-stream",
        is_public=is_public,
        uploaded_by=current_user.id,
        ip_address=ip,
        user_agent=ua,
        base_url=str(request.base_url),
    )
    return created("Media uploaded successfully", MediaOut.model_validate(media))


@router.get("/{media_id}/content", include_in_schema=True)
async def get_media_content(
    media_id: UUID, db: DBSession, user: OptionalUser
):
    """Stream the stored bytes.

    This is what makes ``media.url`` renderable: uploads without a public CDN
    domain point here. Public media is served anonymously; private media
    requires the uploader, the owning member, or an admin.
    """
    media = await MediaRepository(db).get_by_id(media_id)
    if media is None or media.is_deleted:
        raise NotFoundException("Media", str(media_id))

    if not media.is_public and not await _can_access_private(db, media, user):
        raise ForbiddenException()

    content = MediaService(db).read(media)
    return StreamingResponse(
        iter([content]),
        media_type=media.mime_type,
        headers={
            "Content-Length": str(len(content)),
            "Cache-Control": (
                "public, max-age=31536000, immutable"
                if media.is_public
                else "private, no-store"
            ),
            # Never let a stored file be interpreted as active content.
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{media.file_name}"',
        },
    )


async def _can_access_private(db: DBSession, media: Media, user: User | None) -> bool:
    if user is None:
        return False
    if media.uploaded_by is not None and str(media.uploaded_by) == str(user.id):
        return True
    role = getattr(user, "role", None)
    if role is not None and role.name != RoleName.MEMBER.value:
        return True
    if media.owner_type == "member":
        member_user_id = await db.scalar(
            select(Member.user_id).where(Member.id == media.owner_id)
        )
        return member_user_id is not None and str(member_user_id) == str(user.id)
    return False


@router.get("/{media_id}")
async def get_media(media_id: UUID, db: DBSession):
    media = await MediaRepository(db).get_by_id(media_id)
    if media is None:
        raise NotFoundException("Media", str(media_id))
    return ok("Media fetched successfully", MediaOut.model_validate(media))


@router.delete("/{media_id}", dependencies=[DELETE])
async def delete_media(
    media_id: UUID,
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
):
    repo = MediaRepository(db)
    media = await repo.get_by_id(media_id)
    if media is None:
        raise NotFoundException("Media", str(media_id))
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    await MediaService(db).delete(
        media, user_id=current_user.id, ip_address=ip, user_agent=ua
    )
    await db.flush()
    return ok("Media deleted successfully")
