from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import MemberStatus
from app.schemas.family import FamilyMemberOut
from app.schemas.professional import ProfessionalInfoOut
from app.schemas.profile import ProfileOut, ProfileUpdate
from app.schemas.user import UserOut


class MemberCreate(ProfileUpdate):
    """Create payload for ``POST /api/v1/members``.

    Accepts the ``members`` columns plus every profile field of
    :class:`ProfileUpdate`, so a member and their profile data are created in a
    single request. ``full_name`` and ``phone_number`` are required; the other
    profile fields are optional and stored in ``members.profile_data``.
    """

    model_config = ConfigDict(extra="ignore")

    user_id: UUID | None = None
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

    full_name: str = Field(min_length=1)
    phone_number: str = Field(min_length=1)

    def member_columns(self) -> dict[str, Any]:
        """Values for the ``members`` table, without the profile fields."""
        return self.model_dump(
            exclude=set(ProfileUpdate.model_fields), exclude_none=True
        )

    def profile_values(self) -> dict[str, str | None]:
        """Profile fields supplied by the client (dates as ISO strings)."""
        return {
            name: getattr(self, name)
            for name in ProfileUpdate.model_fields
            if name in self.model_fields_set
        }


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
    user_id: UUID | None = None
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


class MemberListItemOut(MemberOut, ProfileOut):
    """List item for ``GET /api/v1/members``: the member record plus the
    profile fields stored in ``members.profile_data`` (or derived from the
    member's columns, family, professional and location relations)."""
