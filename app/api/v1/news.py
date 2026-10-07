from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.constants import AuditAction, NewsStatus
from app.core.exceptions import ForbiddenException, NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import (
    assert_group_access,
    get_access_scope,
    resolve_managed_group,
)
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.news import News
from app.models.user import User
from app.repositories.news_repository import NewsRepository
from app.schemas.news import NewsCreate, NewsOut, NewsPublishRequest, NewsUpdate
from app.services.audit_service import AuditService

router = APIRouter(prefix="/news", tags=["News"])

READ = Depends(require_permission(Permission.NEWS_READ))
CREATE = Depends(require_permission(Permission.NEWS_CREATE))
UPDATE = Depends(require_permission(Permission.NEWS_UPDATE))
DELETE = Depends(require_permission(Permission.NEWS_DELETE))
PUBLISH = Depends(require_permission(Permission.NEWS_PUBLISH))


@router.get("", dependencies=[READ])
async def list_news(
    db: DBSession,
    current_user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    published_only: bool = Query(default=True),
):
    repo = NewsRepository(db)
    scope = await get_access_scope(db, current_user)
    if group_id is not None and not scope.unrestricted and group_id != scope.group_id:
        raise ForbiddenException("Operation is limited to your own group")

    filters: dict[str, object] = {}
    if published_only:
        filters["status"] = NewsStatus.PUBLISHED.value
    if scope.unrestricted:
        if group_id is not None:
            filters["group_id"] = group_id
    else:
        # Forced to the caller's group. Platform-wide (NULL) news is a SUPER_ADMIN
        # concern; a GROUP_ADMIN cannot be shown an item that belongs to no group.
        filters["group_id"] = scope.group_id

    stmt = repo.apply_filters(select(News), filters)
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(News.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "News fetched successfully",
        [NewsOut.model_validate(n) for n in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("", dependencies=[CREATE])
async def create_news(body: NewsCreate, current_user: CurrentUser, db: DBSession):
    data = body.model_dump()
    data["group_id"] = await resolve_managed_group(db, current_user, body.group_id)
    if data["group_id"] is None and not await _is_unrestricted(db, current_user):
        # A GROUP_ADMIN with no resolvable scope would otherwise create an
        # unscoped, invisible item.
        raise ForbiddenException("Your account is not associated with a group")
    if body.status == NewsStatus.PUBLISHED:
        data["published_at"] = datetime.now(UTC)
    news = await NewsRepository(db).create(author_id=current_user.id, **data)
    await AuditService(db).log(
        AuditAction.NEWS_CREATE,
        user_id=current_user.id,
        entity_type="news",
        entity_id=news.id,
    )
    await db.flush()
    return created("News created successfully", NewsOut.model_validate(news))


async def _is_unrestricted(db: DBSession, user: User) -> bool:
    return (await get_access_scope(db, user)).unrestricted


@router.get("/{news_id}", dependencies=[READ])
async def get_news(news_id: UUID, db: DBSession, current_user: CurrentUser):
    item = await NewsRepository(db).get_by_id(news_id)
    if item is None:
        raise NotFoundException("News", str(news_id))
    scope = await get_access_scope(db, current_user)
    # NULL group_id is platform-wide and therefore not visible to a group admin.
    if not scope.unrestricted and item.group_id != scope.group_id:
        raise NotFoundException("News", str(news_id))
    return ok("News fetched successfully", NewsOut.model_validate(item))


@router.patch("/{news_id}", dependencies=[UPDATE])
async def update_news(
    news_id: UUID, body: NewsUpdate, current_user: CurrentUser, db: DBSession
):
    repo = NewsRepository(db)
    news = await repo.get_by_id(news_id)
    if news is None:
        raise NotFoundException("News", str(news_id))
    await assert_group_access(db, current_user, news.group_id)
    data = body.model_dump(exclude_unset=True)
    if "group_id" in data:
        data["group_id"] = await resolve_managed_group(
            db, current_user, data["group_id"]
        )
    if data.get("status") == NewsStatus.PUBLISHED.value and news.published_at is None:
        data["published_at"] = datetime.now(UTC)
    news = await repo.update(news, **data)
    await db.flush()
    await db.refresh(news)
    return ok("News updated successfully", NewsOut.model_validate(news))


@router.post("/{news_id}/publish", dependencies=[PUBLISH])
async def publish_news(
    news_id: UUID,
    body: NewsPublishRequest,
    current_user: CurrentUser,
    db: DBSession,
):
    repo = NewsRepository(db)
    news = await repo.get_by_id(news_id)
    if news is None:
        raise NotFoundException("News", str(news_id))
    await assert_group_access(db, current_user, news.group_id)
    news.status = body.status.value
    if body.status == NewsStatus.PUBLISHED:
        news.published_at = datetime.now(UTC)
    elif body.status == NewsStatus.DRAFT:
        news.published_at = None
    await AuditService(db).log(
        AuditAction.NEWS_PUBLISH,
        user_id=current_user.id,
        entity_type="news",
        entity_id=news.id,
    )
    await db.flush()
    await db.refresh(news)
    return ok("News status updated successfully", NewsOut.model_validate(news))


@router.delete("/{news_id}", dependencies=[DELETE])
async def delete_news(news_id: UUID, current_user: CurrentUser, db: DBSession):
    repo = NewsRepository(db)
    news = await repo.get_by_id(news_id)
    if news is None:
        raise NotFoundException("News", str(news_id))
    await assert_group_access(db, current_user, news.group_id)
    await repo.delete(news)
    return ok("News deleted successfully")
