from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.constants import FeeStatus
from app.utils.helpers import format_to_dd_mm_yyyy, parse_any_date


class FeeCreate(BaseModel):
    title: str
    description: str | None = None
    group_id: UUID | None = None
    financial_year: str
    amount: Decimal
    due_date: date
    status: FeeStatus = FeeStatus.PENDING

    @field_validator("due_date", mode="before")
    @classmethod
    def _parse_due_date(cls, v: Any) -> Any:
        return parse_any_date(v) or v


class FeeUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    group_id: UUID | None = None
    financial_year: str | None = None
    amount: Decimal | None = None
    due_date: date | None = None
    status: FeeStatus | None = None

    @field_validator("due_date", mode="before")
    @classmethod
    def _parse_due_date(cls, v: Any) -> Any:
        return parse_any_date(v) if v else None


class FeeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str | None = None
    group_id: UUID | None = None
    financial_year: str
    amount: Decimal
    due_date: str
    status: str
    is_waived: bool
    created_at: datetime
    updated_at: datetime

    @field_validator("due_date", mode="before")
    @classmethod
    def _format_due_date(cls, v: Any) -> str:
        return format_to_dd_mm_yyyy(v) or str(v)
