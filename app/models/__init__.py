from __future__ import annotations

from app.models.advertisement import Advertisement  # noqa: F401
from app.models.audit_log import AuditLog  # noqa: F401
from app.models.base import Base  # noqa: F401
from app.models.event import Event  # noqa: F401
from app.models.family import FamilyMember  # noqa: F401
from app.models.fee import Fee  # noqa: F401
from app.models.group import SocialGroup  # noqa: F401
from app.models.location import Location  # noqa: F401
from app.models.media import Media  # noqa: F401
from app.models.member import Member  # noqa: F401
from app.models.news import News  # noqa: F401
from app.models.notification import DeviceToken, Notification  # noqa: F401
from app.models.otp_code import OtpCode  # noqa: F401
from app.models.payment import Payment, PaymentTransaction  # noqa: F401
from app.models.professional import ProfessionalInformation  # noqa: F401
from app.models.trustee import Trustee  # noqa: F401
from app.models.user import Permission, Role, RolePermission, User  # noqa: F401
