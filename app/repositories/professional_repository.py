from __future__ import annotations

from app.models.professional import ProfessionalInformation
from app.repositories.base_repository import BaseRepository


class ProfessionalInfoRepository(BaseRepository[ProfessionalInformation]):
    model = ProfessionalInformation
