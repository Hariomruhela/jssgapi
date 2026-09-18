from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.constants import AuditAction, NewsStatus
from app.core.exceptions import NotFoundException
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.news import News
from app.repositories.news_repository import NewsRepository
from app.schemas.news import NewsCreate, NewsOut, NewsPublishRequest, NewsUpdate
from app.services.audit_service import AuditService

router = APIRouter(prefix="/news", tags=["News"])

CREATE = Depends(require_permission(Permission.NEWS_CREATE))
UPDATE = Depends(require_permission(Permission.NEWS_UPDATE))
DELETE = Depends(require_permission(Permission.NEWS_DELETE))
PUBLISH = Depends(require_permission(Permission.NEWS_PUBLISH))


@router.get("")
async def list_news(
    db: DBSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    published_only: bool = Query(default=True),
):
    repo = NewsRepository(db)
    if published_only:
        items = await repo.list_published(
            limit=page_size, offset=(page - 1) * page_size
        )
        total = len(items)
    else:
        stmt = select(News)
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


@router.get("/{news_id}")
async def get_news(news_id: UUID, db: DBSession):
    item = await NewsRepository(db).get_by_id(news_id)
    if item is None:
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
    data = body.model_dump(exclude_unset=True)
    if data.get("status") == NewsStatus.PUBLISHED.value and news.published_at is None:
        data["published_at"] = datetime.now(UTC)
    news = await repo.update(news, **data)
    await db.flush()
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
async def delete_news(news_id: UUID, db: DBSession):
    repo = NewsRepository(db)
    news = await repo.get_by_id(news_id)
    if news is None:
        raise NotFoundException("News", str(news_id))
    await repo.delete(news)
    return ok("News deleted successfully")
