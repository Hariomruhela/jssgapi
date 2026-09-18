from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.core.constants import MemberStatus
from app.schemas.family import FamilyMemberOut
from app.schemas.professional import ProfessionalInfoOut
from app.schemas.user import UserOut


class MemberCreate(BaseModel):
    user_id: UUID
    group_id: UUID | None = None
    location_id: UUID | None = None
    first_name: str
    middle_name: str | None = None
    last_name: str
    gender: str | None = None
    date_of_birth: date | None = None
    blood_group: str | None = None
    profile_photo_url: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    address_line: str | None = None
    occupation_summary: str | None = None
    membership_number: str | None = None


class MemberUpdate(BaseModel):
    group_id: UUID | None = None
    location_id: UUID | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    gender: str | None = None
    date_of_birth: date | None = None
    blood_group: str | None = None
    profile_photo_url: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    address_line: str | None = None
    occupation_summary: str | None = None
    membership_number: str | None = None


class MemberApproveRequest(BaseModel):
    status: MemberStatus
    rejection_reason: str | None = None


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    group_id: UUID | None = None
    location_id: UUID | None = None
    first_name: str
    middle_name: str | None = None
    last_name: str
    gender: str | None = None
    date_of_birth: date | None = None
    blood_group: str | None = None
    profile_photo_url: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    address_line: str | None = None
    occupation_summary: str | None = None
    membership_number: str | None = None
    membership_status: str
    joined_at: datetime | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    is_profile_complete: bool
    user: UserOut | None = None
    family_members: list[FamilyMemberOut] = []
    professional_info: list[ProfessionalInfoOut] = []
    created_at: datetime
    updated_at: datetime
