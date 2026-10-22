from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.group import GroupAdminOut, SocialGroupOut
from app.schemas.member import MemberListItemOut


class SearchPagination(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class MemberSearchSection(BaseModel):
    items: list[MemberListItemOut]
    pagination: SearchPagination


class GroupSearchItemOut(SocialGroupOut):
    """A group hit: the stored group plus its location name and member count."""

    model_config = ConfigDict(from_attributes=True)

    location_name: str | None = None
    member_count: int = 0


# SocialGroupOut inherits `admins: list[GroupAdminOut]`, whose forward reference
# resolves against the module that *defines* the subclass. Subclassing here puts
# the field in this module's namespace, so it is resolved explicitly.
GroupSearchItemOut.model_rebuild(
    _types_namespace={"GroupAdminOut": GroupAdminOut},
)


class GroupSearchSection(BaseModel):
    items: list[GroupSearchItemOut]
    pagination: SearchPagination


class SearchData(BaseModel):
    members: MemberSearchSection | None = None
    groups: GroupSearchSection | None = None


class SearchQuery(BaseModel):
    q: str
    type: str
    group_id: UUID | None = None
    group_name: str | None = None
    location: str | None = None
    status: str | None = None
