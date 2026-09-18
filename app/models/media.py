from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class Media(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "media"

    owner_type: Mapped[str] = mapped_column(String(50), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(100), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    r2_object_key: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    width: Mapped[int | None] = mapped_column(nullable=True)
    height: Mapped[int | None] = mapped_column(nullable=True)
    is_public: Mapped[bool] = mapped_column(default=True, nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    uploader: Mapped[User | None] = relationship()

    def __repr__(self) -> str:
        return f"<Media {self.file_name}>"
