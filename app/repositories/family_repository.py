from __future__ import annotations

from app.models.family import FamilyMember
from app.repositories.base_repository import BaseRepository


class FamilyMemberRepository(BaseRepository[FamilyMember]):
    model = FamilyMember
