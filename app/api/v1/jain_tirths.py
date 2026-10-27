from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BeforeValidator

from app.core.constants import AuditAction, EventStatus, RoleName, resolve_role_name
from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import (
    assert_group_access,
    get_access_scope,
    resolve_managed_group,
)
from app.dependencies import CurrentUser, DBSession, get_current_user, require_permission
from app.models.jain_tirth import JainTirth
from app.models.user import User
from app.repositories.jain_tirth_repository import JainTirthRepository
from app.schemas.jain_tirth import (
    JainTirthCreate,
    JainTirthDetailOut,
    JainTirthImageOut,
    JainTirthOut,
    JainTirthUpdate,
)
from app.services.audit_service import AuditService
from app.services.jain_tirth_image_service import (
    MAX_IMAGES_PER_TIRTH,
    ImageUpload,
    JainTirthImageService,
)
from app.services.jain_tirth_service import JainTirthService

router = APIRouter(prefix="/jain-tirths", tags=["Jain Tirths"])

AUTHENTICATED = Depends(get_current_user)
READ = Depends(require_permission(Permission.JAIN_TIRTH_READ))
CREATE = Depends(require_permission(Permission.JAIN_TIRTH_CREATE))
UPDATE = Depends(require_permission(Permission.JAIN_TIRTH_UPDATE))
DELETE = Depends(require_permission(Permission.JAIN_TIRTH_DELETE))


def _upper(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


StatusQuery = Annotated[
    EventStatus | None,
    BeforeValidator(_upper),
    Query(description="DRAFT, PUBLISHED, CANCELLED or COMPLETED"),
]


@dataclass(frozen=True)
class ReadScope:
    group_id: UUID | None = None
    ungrouped_only: bool = False
    include_ungrouped: bool = True
    public_only: bool = False


def _role_of(user: User) -> RoleName | None:
    if user.role is None:
        return None
    return resolve_role_name(user.role.name)


async def _read_scope(
    db: DBSession, current_user: User, group_id: UUID | None
) -> ReadScope:
    return ReadScope(group_id=group_id)


def _tirth_out(tirth: JainTirth, image_count: int = 0) -> JainTirthOut:
    out = JainTirthOut.model_validate(tirth)
    out.image_count = image_count
    return out


async def _get_tirth_or_404(db: DBSession, tirth_id: UUID) -> JainTirth:
    tirth = await JainTirthRepository(db).get_by_id(tirth_id)
    if tirth is None:
        raise NotFoundException("Jain Tirth", str(tirth_id))
    return tirth


async def _get_visible_tirth(
    db: DBSession, current_user: User, tirth_id: UUID
) -> JainTirth:
    tirth = await _get_tirth_or_404(db, tirth_id)
    scope = await get_access_scope(db, current_user)
    role = _role_of(current_user)

    if scope.unrestricted:
        return tirth

    if role is RoleName.GROUP_ADMIN:
        if tirth.group_id is not None and tirth.group_id != scope.group_id:
            raise NotFoundException("Jain Tirth", str(tirth_id))
        return tirth

    if not tirth.is_public:
        raise NotFoundException("Jain Tirth", str(tirth_id))
    if scope.group_id is not None and tirth.group_id not in (
        None,
        scope.group_id,
    ):
        raise NotFoundException("Jain Tirth", str(tirth_id))
    return tirth


@router.get("", dependencies=[AUTHENTICATED])
async def list_jain_tirths(
    db: DBSession,
    current_user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    status: StatusQuery = None,
    search: str | None = Query(default=None, max_length=255),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = JainTirthRepository(db)
    scope = await _read_scope(db, current_user, group_id)

    total = await repo.count_filtered(
        group_id=scope.group_id,
        ungrouped_only=scope.ungrouped_only,
        include_ungrouped=scope.include_ungrouped,
        public_only=scope.public_only,
        status=status,
        search=search,
    )
    items = await repo.list_filtered(
        group_id=scope.group_id,
        ungrouped_only=scope.ungrouped_only,
        include_ungrouped=scope.include_ungrouped,
        public_only=scope.public_only,
        status=status,
        search=search,
        order=order,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    images = JainTirthImageService(db)
    counts = await images.batch_count_images(items)
    data = [_tirth_out(t, counts.get(t.id, 0)) for t in items]
    return paginated(
        "Jain Tirths fetched successfully",
        data,
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.get("/{tirth_id}", dependencies=[READ])
async def get_jain_tirth(
    tirth_id: UUID, db: DBSession, current_user: CurrentUser
):
    tirth = await _get_visible_tirth(db, current_user, tirth_id)
    images = await JainTirthImageService(db).list_images(tirth)
    detail = JainTirthDetailOut.model_validate(tirth)
    detail.images = [JainTirthImageOut.model_validate(i) for i in images]
    detail.image_count = len(images)
    return ok("Jain Tirth fetched successfully", detail)


@router.post("", dependencies=[CREATE])
async def create_jain_tirth(
    body: JainTirthCreate, current_user: CurrentUser, db: DBSession
):
    data = body.model_dump()
    data["group_id"] = await resolve_managed_group(
        db, current_user, body.group_id
    )
    data["status"] = body.status.value

    tirth = JainTirth(**data)
    db.add(tirth)
    await db.flush()
    await db.refresh(tirth)

    await AuditService(db).log(
        AuditAction.JAIN_TIRTH_CREATE,
        user_id=current_user.id,
        entity_type="jain_tirth",
        entity_id=tirth.id,
    )
    await db.flush()
    return created("Jain Tirth created successfully", _tirth_out(tirth))


@router.patch("/{tirth_id}", dependencies=[UPDATE])
async def update_jain_tirth(
    tirth_id: UUID,
    body: JainTirthUpdate,
    current_user: CurrentUser,
    db: DBSession,
):
    repo = JainTirthRepository(db)
    tirth = await repo.get_by_id(tirth_id)
    if tirth is None:
        raise NotFoundException("Jain Tirth", str(tirth_id))
    await assert_group_access(db, current_user, tirth.group_id)
    if body.group_id is not None:
        await assert_group_access(db, current_user, body.group_id)

    data = body.model_dump(exclude_unset=True, exclude_none=True)
    if body.status is not None:
        data["status"] = body.status.value
    for key, value in data.items():
        setattr(tirth, key, value)

    tirth = await repo.update(tirth)
    await AuditService(db).log(
        AuditAction.JAIN_TIRTH_UPDATE,
        user_id=current_user.id,
        entity_type="jain_tirth",
        entity_id=tirth.id,
    )
    await db.flush()
    return ok("Jain Tirth updated successfully", _tirth_out(tirth))


@router.delete("/{tirth_id}", dependencies=[DELETE])
async def delete_jain_tirth(
    tirth_id: UUID, current_user: CurrentUser, db: DBSession
):
    repo = JainTirthRepository(db)
    tirth = await repo.get_by_id(tirth_id)
    if tirth is None:
        raise NotFoundException("Jain Tirth", str(tirth_id))
    await assert_group_access(db, current_user, tirth.group_id)

    await JainTirthImageService(db).delete_all_for_tirth(tirth)
    await JainTirthService(db).remove_cover(tirth)
    await repo.delete(tirth)
    await AuditService(db).log(
        AuditAction.JAIN_TIRTH_DELETE,
        user_id=current_user.id,
        entity_type="jain_tirth",
        entity_id=tirth.id,
    )
    await db.flush()
    return ok("Jain Tirth deleted successfully")


@router.post("/{tirth_id}/cover", dependencies=[UPDATE])
async def upload_jain_tirth_cover(
    tirth_id: UUID,
    request: Request,
    db: DBSession,
    current_user: CurrentUser,
    file: UploadFile = File(...),
):
    tirth = await _get_tirth_or_404(db, tirth_id)
    await assert_group_access(db, current_user, tirth.group_id)

    content = await file.read()
    url = await JainTirthService(db).upload_cover(
        tirth,
        file_name=file.filename,
        content=content,
        mime_type=file.content_type,
        uploaded_by=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        base_url=str(request.base_url),
    )
    await AuditService(db).log(
        AuditAction.JAIN_TIRTH_IMAGE_UPLOAD,
        user_id=current_user.id,
        entity_type="jain_tirth",
        entity_id=tirth.id,
        details={"field": "cover_image", "url": url},
    )
    await db.flush()
    await db.refresh(tirth)
    return created("Jain Tirth cover image uploaded successfully", _tirth_out(tirth))


@router.delete("/{tirth_id}/cover", dependencies=[UPDATE])
async def delete_jain_tirth_cover(
    tirth_id: UUID, current_user: CurrentUser, db: DBSession
):
    tirth = await _get_tirth_or_404(db, tirth_id)
    await assert_group_access(db, current_user, tirth.group_id)
    await JainTirthService(db).remove_cover(tirth)
    await db.flush()
    await db.refresh(tirth)
    return ok("Jain Tirth cover image deleted successfully", _tirth_out(tirth))


@router.get("/{tirth_id}/images", dependencies=[AUTHENTICATED])
async def list_jain_tirth_images(
    tirth_id: UUID, db: DBSession, current_user: CurrentUser
):
    tirth = await _get_tirth_or_404(db, tirth_id)
    images = await JainTirthImageService(db).list_images(tirth)
    return ok(
        "Jain Tirth images fetched successfully",
        [JainTirthImageOut.model_validate(i) for i in images],
    )


@router.post("/{tirth_id}/images", dependencies=[UPDATE])
async def upload_jain_tirth_images(
    tirth_id: UUID,
    request: Request,
    db: DBSession,
    current_user: CurrentUser,
    files: list[UploadFile] = File(...),
):
    tirth = await _get_tirth_or_404(db, tirth_id)
    await assert_group_access(db, current_user, tirth.group_id)

    if not files:
        raise BadRequestException("At least one image is required", "NO_IMAGES")
    if len(files) > MAX_IMAGES_PER_TIRTH:
        raise BadRequestException(
            f"A Jain Tirth can hold at most {MAX_IMAGES_PER_TIRTH} images",
            "TOO_MANY_IMAGES",
        )

    uploads = [
        ImageUpload(
            file_name=f.filename,
            content=await f.read(),
            mime_type=f.content_type,
        )
        for f in files
    ]

    images = await JainTirthImageService(db).upload_images(
        tirth,
        uploads,
        uploaded_by=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        base_url=str(request.base_url),
    )
    await AuditService(db).log(
        AuditAction.JAIN_TIRTH_IMAGE_UPLOAD,
        user_id=current_user.id,
        entity_type="jain_tirth",
        entity_id=tirth.id,
        details={"count": len(images)},
    )
    await db.flush()
    return created(
        "Jain Tirth images uploaded successfully",
        [JainTirthImageOut.model_validate(i) for i in images],
    )


@router.delete("/{tirth_id}/images/{image_id}", dependencies=[UPDATE])
async def delete_jain_tirth_image(
    tirth_id: UUID,
    image_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
):
    tirth = await _get_tirth_or_404(db, tirth_id)
    await assert_group_access(db, current_user, tirth.group_id)

    service = JainTirthImageService(db)
    image = await service.find_image(tirth, image_id)
    await service.delete_image(tirth, image)
    await db.flush()
    return ok("Jain Tirth image deleted successfully")
