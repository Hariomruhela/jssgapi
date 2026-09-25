from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = None
    member_dob: str | None = None
    member_photo_link: str | None = None
    spouse_name: str | None = None
    spouse_mobile: str | None = None
    spouse_dob: str | None = None
    spouse_photo_link: str | None = None
    spouse_education: str | None = None
    spouse_occupation: str | None = None
    anniversary_date: str | None = None
    family_member_count: str | None = None
    son_name: str | None = None
    son_dob: str | None = None
    son_education: str | None = None
    son_occupation: str | None = None
    daughter_name: str | None = None
    daughter_dob: str | None = None
    daughter_education: str | None = None
    daughter_occupation: str | None = None
    member_education: str | None = None
    member_occupation: str | None = None
    company_name: str | None = None
    group_designation: str | None = None
    social_group_name: str | None = None
    interest_fields: str | None = None
    address: str | None = None
    city: str | None = None
    area: str | None = None
    phone_number: str | None = None
    email: str | None = None


class ProfileOut(ProfileUpdate):
    pass


class ProfileResponse(BaseModel):
    success: bool = True
    profile: ProfileOut


class ProfilePhotoResponse(BaseModel):
    success: bool = True
    photo_link: str
