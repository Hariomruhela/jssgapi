from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import AuditAction
from app.core.exceptions import ProfileNotFoundException
from app.models.family import FamilyMember
from app.models.member import Member
from app.models.professional import ProfessionalInformation
from app.schemas.profile import ProfileOut
from app.services.audit_service import AuditService
from app.services.media_service import MediaService

PROFILE_FIELDS = (
    "full_name",
    "member_dob",
    "member_photo_link",
    "spouse_name",
    "spouse_mobile",
    "spouse_dob",
    "spouse_photo_link",
    "spouse_education",
    "spouse_occupation",
    "anniversary_date",
    "family_member_count",
    "son_name",
    "son_dob",
    "son_education",
    "son_occupation",
    "daughter_name",
    "daughter_dob",
    "daughter_education",
    "daughter_occupation",
    "member_education",
    "member_occupation",
    "company_name",
    "group_designation",
    "social_group_name",
    "interest_fields",
    "address",
    "city",
    "area",
    "phone_number",
    "email",
)


def _as_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _set_string_value(
    target: Any, attribute: str, value: str | None, max_length: int | None = None
) -> None:
    if max_length is not None and value is not None and len(value) > max_length:
        return
    setattr(target, attribute, value)


class ProfileService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.audit = AuditService(session)

    async def _get_member(self, user_id: UUID) -> Member | None:
        result = await self.session.execute(
            select(Member)
            .options(
                selectinload(Member.user),
                selectinload(Member.group),
                selectinload(Member.location),
                selectinload(Member.family_members),
                selectinload(Member.professional_info),
            )
            .where(Member.user_id == user_id, Member.is_deleted.is_(False))
        )
        return result.scalar_one_or_none()

    async def get_profile(self, user_id: UUID) -> ProfileOut:
        member = await self._get_member(user_id)
        if member is None:
            raise ProfileNotFoundException()
        return self._build_profile(member)

    async def update_profile(
        self,
        user_id: UUID,
        values: dict[str, str | None],
    ) -> ProfileOut:
        member = await self._get_member(user_id)
        if member is None:
            raise ProfileNotFoundException()

        profile_data = dict(member.profile_data or {})
        profile_data.update(values)
        member.profile_data = profile_data
        self._apply_member_fields(member, values)
        await self._sync_family_fields(member, values)
        await self._sync_professional_fields(member, values)
        await self.session.flush()
        await self.audit.log(
            AuditAction.MEMBER_UPDATE,
            user_id=user_id,
            entity_type="member",
            entity_id=member.id,
            details={"fields": sorted(values)},
        )
        return self._build_profile(member)

    async def upload_photo(
        self,
        user_id: UUID,
        *,
        file_name: str,
        content: bytes,
        mime_type: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> str:
        member = await self._get_member(user_id)
        if member is None:
            raise ProfileNotFoundException()
        media = await MediaService(self.session).upload(
            owner_type="member",
            owner_id=member.id,
            file_name=file_name,
            content=content,
            mime_type=mime_type,
            is_public=True,
            uploaded_by=user_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        member.profile_photo_url = media.url
        profile_data = dict(member.profile_data or {})
        profile_data["member_photo_link"] = media.url
        member.profile_data = profile_data
        await self.session.flush()
        return media.url

    def _build_profile(self, member: Member) -> ProfileOut:
        values = self._fallback_values(member)
        for field, value in (member.profile_data or {}).items():
            if field in PROFILE_FIELDS:
                values[field] = _as_string(value)
        return ProfileOut(**values)

    def _fallback_values(self, member: Member) -> dict[str, str | None]:
        user = member.user
        spouse = self._family_member(member, {"spouse", "wife", "husband"})
        son = self._family_member(member, {"son"})
        daughter = self._family_member(member, {"daughter"})
        professional = self._professional_info(member)
        location = member.location
        if location is not None and location.is_deleted:
            location = None
        group = member.group
        if group is not None and group.is_deleted:
            group = None

        full_name = user.full_name if user is not None else None
        if not full_name:
            full_name = " ".join(
                part
                for part in (member.first_name, member.middle_name, member.last_name)
                if part
            )

        return {
            "full_name": _as_string(full_name),
            "member_dob": _as_string(member.date_of_birth),
            "member_photo_link": member.profile_photo_url,
            "spouse_name": spouse.name if spouse else None,
            "spouse_mobile": spouse.contact_phone if spouse else None,
            "spouse_dob": _as_string(spouse.date_of_birth) if spouse else None,
            "spouse_photo_link": None,
            "spouse_education": None,
            "spouse_occupation": spouse.occupation if spouse else None,
            "anniversary_date": None,
            "family_member_count": str(len(member.family_members)),
            "son_name": son.name if son else None,
            "son_dob": _as_string(son.date_of_birth) if son else None,
            "son_education": None,
            "son_occupation": son.occupation if son else None,
            "daughter_name": daughter.name if daughter else None,
            "daughter_dob": _as_string(daughter.date_of_birth) if daughter else None,
            "daughter_education": None,
            "daughter_occupation": daughter.occupation if daughter else None,
            "member_education": None,
            "member_occupation": member.occupation_summary,
            "company_name": professional.company_name if professional else None,
            "group_designation": professional.designation if professional else None,
            "social_group_name": group.name if group else None,
            "interest_fields": None,
            "address": member.address_line,
            "city": location.city if location else None,
            "area": location.area if location else None,
            "phone_number": member.contact_phone
            or (user.phone_number if user else None),
            "email": member.contact_email or (user.email if user else None),
        }

    @staticmethod
    def _family_member(member: Member, aliases: set[str]) -> FamilyMember | None:
        for item in sorted(
            member.family_members,
            key=lambda value: value.created_at.isoformat() if value.created_at else "",
        ):
            if item.relationship_type.strip().casefold() in aliases:
                return item
        return None

    @staticmethod
    def _professional_info(member: Member) -> ProfessionalInformation | None:
        if not member.professional_info:
            return None
        return sorted(
            member.professional_info,
            key=lambda value: value.created_at.isoformat() if value.created_at else "",
        )[0]

    def _apply_member_fields(
        self, member: Member, values: dict[str, str | None]
    ) -> None:
        if "full_name" in values and values["full_name"] is not None and member.user:
            _set_string_value(member.user, "full_name", values["full_name"], 255)
        if "member_dob" in values:
            if values["member_dob"] is None:
                member.date_of_birth = None
            else:
                parsed = _parse_date(values["member_dob"])
                if parsed is not None:
                    member.date_of_birth = parsed
        if "member_photo_link" in values:
            member.profile_photo_url = values["member_photo_link"]
        if "member_occupation" in values:
            member.occupation_summary = values["member_occupation"]
        if "address" in values:
            member.address_line = values["address"]
        if "phone_number" in values:
            _set_string_value(member, "contact_phone", values["phone_number"], 20)
        if "email" in values:
            _set_string_value(member, "contact_email", values["email"], 255)

    async def _sync_family_fields(
        self, member: Member, values: dict[str, str | None]
    ) -> None:
        definitions = (
            ("spouse", {"spouse", "wife", "husband"}, "spouse"),
            ("son", {"son"}, "son"),
            ("daughter", {"daughter"}, "daughter"),
        )
        for relationship, aliases, prefix in definitions:
            target = self._family_member(member, aliases)
            name_key = f"{prefix}_name"
            name = values.get(name_key)
            if name is not None:
                if target is None:
                    if len(name) <= 255:
                        target = FamilyMember(
                            member_id=member.id,
                            name=name,
                            relationship_type=relationship,
                        )
                        self.session.add(target)
                        member.family_members.append(target)
                else:
                    _set_string_value(target, "name", name, 255)
            if target is None:
                continue
            for suffix, attribute in (
                ("dob", "date_of_birth"),
                ("occupation", "occupation"),
                ("mobile", "contact_phone"),
            ):
                key = f"{prefix}_{suffix}"
                if key not in values:
                    continue
                value = values[key]
                if attribute == "date_of_birth":
                    if value is None:
                        target.date_of_birth = None
                    else:
                        parsed = _parse_date(value)
                        if parsed is not None:
                            target.date_of_birth = parsed
                else:
                    _set_string_value(
                        target,
                        attribute,
                        value,
                        20 if attribute == "contact_phone" else 255,
                    )

    async def _sync_professional_fields(
        self, member: Member, values: dict[str, str | None]
    ) -> None:
        mapping = {
            "member_occupation": "occupation",
            "company_name": "company_name",
            "group_designation": "designation",
        }
        if not any(key in values for key in mapping):
            return
        target = self._professional_info(member)
        if target is None and any(values.get(key) is not None for key in mapping):
            target = ProfessionalInformation(
                member_id=member.id,
                is_visible_in_directory=True,
            )
            self.session.add(target)
            member.professional_info.append(target)
        if target is None:
            return
        for key, attribute in mapping.items():
            if key in values:
                _set_string_value(target, attribute, values[key], 255)
