from __future__ import annotations

from enum import Enum


class RoleName(str, Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    FEDERATION_ADMIN = "FEDERATION_ADMIN"
    ADMIN = "ADMIN"
    REGIONAL_ADMIN = "REGIONAL_ADMIN"
    GROUP_ADMIN = "GROUP_ADMIN"
    MEMBER = "MEMBER"


# Canonical lookup: the six RoleName values are the ONLY roles the system recognises.
# The `roles` table is populated from external sources (Excel / Google Sheet imports),
# which have historically stored whitespace-polluted labels such as "Admin\n". Those rows
# matched no ROLE_PERMISSIONS key, so legitimate admins silently lost every permission.
# Resolving the stored label back to the canonical enum keeps exactly one role identity
# per name instead of loosening authorization: "admin" can only ever resolve to ADMIN.
_CANONICAL_ROLES: dict[str, RoleName] = {
    role.value.casefold(): role for role in RoleName
}


def resolve_role_name(raw: str | None) -> RoleName | None:
    """Map a stored/incoming role label onto its canonical RoleName, or None if unknown."""
    if raw is None:
        return None
    return _CANONICAL_ROLES.get(str(raw).strip().casefold())


class MemberStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    INACTIVE = "INACTIVE"


class EventStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class NewsStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    UNPUBLISHED = "UNPUBLISHED"
    SCHEDULED = "SCHEDULED"


class AdvertisementStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    CANCELLED = "CANCELLED"


class MediaType(str, Enum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    DOCUMENT = "DOCUMENT"


class AuditAction(str, Enum):
    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    REGISTER = "REGISTER"
    PASSWORD_CHANGE = "PASSWORD_CHANGE"
    OTP_REQUEST = "OTP_REQUEST"
    OTP_VERIFY = "OTP_VERIFY"
    OTP_FAILED = "OTP_FAILED"
    MEMBER_CREATE = "MEMBER_CREATE"
    MEMBER_UPDATE = "MEMBER_UPDATE"
    MEMBER_APPROVE = "MEMBER_APPROVE"
    MEMBER_REJECT = "MEMBER_REJECT"
    MEMBER_BLOCK = "MEMBER_BLOCK"
    GROUP_CREATE = "GROUP_CREATE"
    GROUP_UPDATE = "GROUP_UPDATE"
    GROUP_DELETE = "GROUP_DELETE"
    EVENT_CREATE = "EVENT_CREATE"
    EVENT_UPDATE = "EVENT_UPDATE"
    NEWS_CREATE = "NEWS_CREATE"
    NEWS_PUBLISH = "NEWS_PUBLISH"
    ADVERTISEMENT_APPROVE = "ADVERTISEMENT_APPROVE"
    PAYMENT_CREATE = "PAYMENT_CREATE"
    PAYMENT_VERIFY = "PAYMENT_VERIFY"
    ROLE_CHANGE = "ROLE_CHANGE"
    MEDIA_UPLOAD = "MEDIA_UPLOAD"
    MEDIA_DELETE = "MEDIA_DELETE"
    MEDIA_MIGRATE = "MEDIA_MIGRATE"


class FeeStatus(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    OVERDUE = "OVERDUE"
    WAIVED = "WAIVED"
