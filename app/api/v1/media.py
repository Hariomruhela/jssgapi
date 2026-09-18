from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile

from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok
from app.dependencies import CurrentUser, DBSession, require_permission
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
    )
    return created("Media uploaded successfully", MediaOut.model_validate(media))


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
