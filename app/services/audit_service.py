from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import AuditAction
from app.models.audit_log import AuditLog


class AuditService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def log(
        self,
        action: str | AuditAction,
        *,
        user_id=None,
        entity_type: str | None = None,
        entity_id=None,
        details: dict | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            user_id=user_id,
            action=action.value if isinstance(action, AuditAction) else action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry
