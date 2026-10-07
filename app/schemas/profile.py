from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

# Request aliases only: responses always use the canonical field name, so
# existing clients keep seeing the same keys.


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = None
    member_dob: str | None = None
    member_photo_link: str | None = Field(
        default=None,
        validation_alias=AliasChoices("member_photo_link", "member_photo_url"),
    )
    spouse_name: str | None = None
    spouse_mobile: str | None = None
    spouse_dob: str | None = None
    spouse_photo_link: str | None = Field(
        default=None,
        validation_alias=AliasChoices("spouse_photo_link", "spouse_photo_url"),
    )
    spouse_education: str | None = None
    spouse_occupation: str | None = None
    anniversary_date: str | None = None
    family_member_count: int | str | None = None
    son_name: str | None = None
    son_dob: str | None = None
    son_education: str | None = None
    son_occupation: str | None = None
    daughter_name: str | None = None
    daughter_dob: str | None = None
    daughter_education: str | None = None
    daughter_occupation: str | None = None
    unmarried_son1_details: str | None = None
    unmarried_son1_dob: str | None = None
    unmarried_son1_education: str | None = None
    unmarried_son1_occupation: str | None = None
    unmarried_son2_details: str | None = None
    unmarried_son2_dob: str | None = None
    unmarried_son2_education: str | None = None
    unmarried_son2_occupation: str | None = None
    unmarried_son3_details: str | None = None
    unmarried_son3_dob: str | None = None
    unmarried_son3_education: str | None = None
    unmarried_son3_occupation: str | None = None
    unmarried_daughter1_details: str | None = None
    unmarried_daughter1_dob: str | None = None
    unmarried_daughter1_education: str | None = None
    unmarried_daughter1_occupation: str | None = None
    unmarried_daughter2_details: str | None = None
    unmarried_daughter2_dob: str | None = None
    unmarried_daughter2_education: str | None = None
    unmarried_daughter2_occupation: str | None = None
    unmarried_daughter3_details: str | None = None
    unmarried_daughter3_dob: str | None = None
    unmarried_daughter3_education: str | None = None
    unmarried_daughter3_occupation: str | None = None
    member_education: str | None = None
    member_occupation: str | None = None
    company_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("company_name", "company_business_name"),
    )
    group_designation: str | None = None
    social_group_name: str | None = None
    interest_fields: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "interest_fields", "interest_field_of_social_activities"
        ),
    )
    business_address: str | None = None
    address: str | None = None
    city: str | None = None
    area: str | None = None
    phone_number: str | None = Field(
        default=None,
        validation_alias=AliasChoices("phone_number", "mobile_number"),
    )
    email: str | None = None


class ProfileOut(ProfileUpdate):
    pass


class ProfileResponse(BaseModel):
    success: bool = True
    profile: ProfileOut


class ProfilePhotoResponse(BaseModel):
    success: bool = True
    photo_link: str
