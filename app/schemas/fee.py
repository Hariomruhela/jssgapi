from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.core.constants import FeeStatus


class FeeCreate(BaseModel):
    title: str
    description: str | None = None
    group_id: UUID | None = None
    financial_year: str
    amount: Decimal
    due_date: date
    status: FeeStatus = FeeStatus.PENDING


class FeeUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    group_id: UUID | None = None
    financial_year: str | None = None
    amount: Decimal | None = None
    due_date: date | None = None
    status: FeeStatus | None = None


class FeeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str | None = None
    group_id: UUID | None = None
    financial_year: str
    amount: Decimal
    due_date: date
    status: str
    is_waived: bool
    created_at: datetime
    updated_at: datetime
