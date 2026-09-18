from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID | None = None
    action: str
    entity_type: str | None = None
    entity_id: UUID | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    details: dict | None = None
    created_at: datetime
