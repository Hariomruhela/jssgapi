from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.utils.helpers import format_to_dd_mm_yyyy


class CelebrationPersonOut(BaseModel):
    """A member or their spouse, as shown on a celebration card.

    ``photo_url`` is the same URL the member directory uses, and ``id`` is only
    set for a real member row (a spouse read from free text has no row id).
    """

    id: UUID | None = None
    full_name: str | None = None
    photo_url: str | None = None
    date_of_birth: str | None = None

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def _format_dob(cls, v: Any) -> str | None:
        return format_to_dd_mm_yyyy(v)


class CelebrationLocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    city: str | None = None
    area: str | None = None


class BirthdayOut(BaseModel):
    """One birthday happening today.

    ``occasion`` says whose birthday it is; ``member`` and ``spouse`` always
    carry both people so the app can render the pair in one card.
    """

    occasion: Literal["member_birthday", "spouse_birthday"]
    date: str
    member: CelebrationPersonOut
    spouse: CelebrationPersonOut | None = None
    group_id: UUID | None = None
    group_name: str | None = None
    location: CelebrationLocationOut | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _format_date(cls, v: Any) -> str:
        return format_to_dd_mm_yyyy(v) or str(v)


class AnniversaryOut(BaseModel):
    """One wedding anniversary happening today."""

    occasion: Literal["anniversary"] = "anniversary"
    date: str
    years: int | None = None
    member: CelebrationPersonOut
    spouse: CelebrationPersonOut | None = None
    group_id: UUID | None = None
    group_name: str | None = None
    location: CelebrationLocationOut | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _format_date(cls, v: Any) -> str:
        return format_to_dd_mm_yyyy(v) or str(v)
