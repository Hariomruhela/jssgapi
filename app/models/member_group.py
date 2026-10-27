from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.group import SocialGroup
    from app.models.member import Member


class MemberGroupMembership(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A member's membership in one social group.

    ``priority`` is one-based and the lowest priority is the member's primary
    group. The primary group owns the visible ``members.membership_number``;
    secondary memberships never change that number.
    """

    __tablename__ = "member_group_memberships"
    __table_args__ = (
        UniqueConstraint(
            "member_id",
            "group_id",
            name="uq_member_group_memberships_member_group",
        ),
        UniqueConstraint(
            "member_id",
            "priority",
            name="uq_member_group_memberships_member_priority",
        ),
        CheckConstraint("priority >= 1", name="priority_positive"),
        Index("ix_member_group_memberships_group_id", "group_id"),
    )

    member_id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("members.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    membership_number: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    member: Mapped[Member] = relationship(back_populates="group_memberships")
    group: Mapped[SocialGroup] = relationship(
        back_populates="member_memberships", lazy="selectin"
    )

    @property
    def is_primary(self) -> bool:
        return self.priority == 1

    @property
    def group_name(self) -> str | None:
        group = self.group
        if group is None or group.is_deleted:
            return None
        return group.name

    @property
    def group_code(self) -> str | None:
        return self.group.group_id if self.group is not None else None


class GroupMember(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Many-to-Many Junction Table for Multi-Group Membership."""

    __tablename__ = "group_members"
    __table_args__ = (
        UniqueConstraint("user_id", "group_id", name="uq_user_group"),
        UniqueConstraint(
            "group_id", "membership_number", name="uq_group_membership_number"
        ),
        Index("idx_group_members_user", "user_id"),
        Index("idx_group_members_group", "group_id"),
        Index("idx_group_members_primary", "user_id", "is_primary"),
    )

    user_id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("social_groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    membership_number: Mapped[str] = mapped_column(String(30), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="MEMBER", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="APPROVED", nullable=False)
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
