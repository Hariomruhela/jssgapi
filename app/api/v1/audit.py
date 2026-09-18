from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.permissions import Permission
from app.core.responses import paginated
from app.dependencies import DBSession, require_permission
from app.models.audit_log import AuditLog
from app.schemas.audit import AuditLogOut

router = APIRouter(prefix="/audit-logs", tags=["Audit"])

READ = Depends(require_permission(Permission.AUDIT_READ))


@router.get("", dependencies=[READ])
async def list_audit_logs(
    db: DBSession,
    action: str | None = Query(default=None),
    user_id: UUID | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(AuditLog.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Audit logs fetched successfully",
        [AuditLogOut.model_validate(log) for log in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.get("/{log_id}", dependencies=[READ])
async def get_audit_log(log_id: UUID, db: DBSession):
    from app.core.exceptions import NotFoundException

    result = await db.execute(select(AuditLog).where(AuditLog.id == log_id))
    log = result.scalar_one_or_none()
    if log is None:
        raise NotFoundException("Audit log", str(log_id))
    from app.core.responses import ok

    return ok("Audit log fetched successfully", AuditLogOut.model_validate(log))
