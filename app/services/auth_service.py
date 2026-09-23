from __future__ import annotations

import hashlib
import hmac as hmac_module
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.constants import AuditAction, MemberStatus, RoleName
from app.core.exceptions import (
    AlreadyExistsException,
    BadRequestException,
    ForbiddenException,
    NotFoundException,
    UnauthorizedException,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    hash_password,
    verify_password,
)
from app.integrations.msg91 import direct_mode, verify_access_token, verify_otp
from app.integrations.msg91 import send_otp as msg91_send_otp
from app.integrations.sms import is_dev_sms, send_otp
from app.models.otp_code import OtpCode
from app.models.user import RefreshToken, Role, User
from app.repositories.member_repository import MemberRepository
from app.repositories.user_repository import UserRepository
from app.services.audit_service import AuditService
from app.utils.helpers import hash_refresh_token
from app.utils.validators import normalize_phone_number

settings = get_settings()


class AuthService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.users = UserRepository(session)
        self.members = MemberRepository(session)
        self.audit = AuditService(session)

    async def send_registration_otp(
        self,
        *,
        phone: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> str:
        """Send an MSG91 OTP for the registration flow.

        Only numbers that are not already registered get an OTP. The OTP is
        generated, delivered, and stored by MSG91 — this server never sees or
        stores it. Returns MSG91's ``req_id``.
        """
        normalized = normalize_phone_number(phone)
        if await self.users.get_by_phone(normalized):
            raise AlreadyExistsException(
                "An account with this phone number already exists"
            )

        req_id = await msg91_send_otp(normalized)
        await self.audit.log(
            AuditAction.OTP_REQUEST,
            entity_type="registration",
            details={"phone": self._mask_phone(normalized)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        return req_id

    async def register(
        self,
        *,
        full_name: str,
        email: str | None,
        password: str,
        phone_number: str,
        role: str | None = None,
        otp: str,
        req_id: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        full_name = full_name.strip()
        if not full_name:
            raise BadRequestException("Full name cannot be empty")
        email = email.strip().lower() if email else None

        phone_normalized = normalize_phone_number(phone_number)
        verified_mobile: str | None
        if direct_mode():
            verified_mobile = await verify_otp(req_id, otp, mobile=phone_normalized)
        else:
            access_token = await verify_otp(req_id, otp)
            verified = await verify_access_token(access_token)
            verified_mobile = verified.get("mobile") if verified else None
        if not verified_mobile:
            raise UnauthorizedException(
                "Mobile number verification failed. Please verify your OTP again."
            )
        verified_phone = normalize_phone_number(str(verified_mobile))
        if verified_phone != phone_normalized:
            raise UnauthorizedException(
                "Verified mobile number does not match the provided phone number"
            )

        if email and await self.users.get_by_email(email):
            raise AlreadyExistsException("An account with this email already exists")
        if await self.users.get_by_phone(phone_normalized):
            raise AlreadyExistsException("An account with this phone number exists")

        role_row = await self._resolve_registration_role(role)
        if role_row is None:
            raise BadRequestException("Role is not configured yet")

        user = await self.users.create(
            email=email,
            full_name=full_name,
            phone_number=phone_normalized,
            password_hash=hash_password(password),
            is_active=True,
            is_phone_verified=True,
            role_id=role_row.id,
        )

        name_parts = full_name.strip().split(maxsplit=1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else name_parts[0]

        await self.members.create(
            user_id=user.id,
            first_name=first_name,
            last_name=last_name,
            contact_email=email,
            contact_phone=phone_normalized,
            membership_status=MemberStatus.PENDING.value,
        )

        await self.audit.log(
            AuditAction.REGISTER,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()

        return await self._issue_tokens(user, ip_address, user_agent)

    async def _resolve_registration_role(self, role: str | None) -> Role:
        if role is None or not role.strip():
            role_name = RoleName.MEMBER.value
        else:
            candidate = role.strip().upper()
            if candidate not in (RoleName.MEMBER.value, RoleName.ADMIN.value):
                raise BadRequestException(
                    "Invalid role. Allowed roles: MEMBER, ADMIN"
                )
            role_name = candidate
        role_result = await self.session.execute(
            select(Role).where(Role.name == role_name)
        )
        role_row = role_result.scalar_one_or_none()
        if role_row is None:
            raise BadRequestException(f"{role_name} role is not configured yet")
        return role_row

    async def login(
        self,
        *,
        email: str | None = None,
        phone_number: str | None = None,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        if not email and not phone_number:
            raise UnauthorizedException("Invalid email or phone number or password")

        if phone_number:
            user = await self.users.get_active_by_phone(
                normalize_phone_number(phone_number)
            )
            credentials = "Invalid phone number or password"
        else:
            assert email is not None
            user = await self.users.get_active_by_email(email.strip().lower())
            credentials = "Invalid email or password"

        if user is None or not verify_password(password, user.password_hash):
            raise UnauthorizedException(credentials)

        user.last_login_at = datetime.now(UTC)
        await self.session.flush()

        await self.audit.log(
            AuditAction.LOGIN,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return await self._issue_tokens(user, ip_address, user_agent)

    async def oauth2_admin_token(
        self,
        *,
        username: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        """OAuth2 password-flow token endpoint used by Swagger's Authorize.

        username is treated as the ADMIN account's phone number. Only ADMIN
        accounts are allowed to authenticate through this flow.
        """
        user = await self.users.get_active_by_phone(
            normalize_phone_number(username)
        )
        if user is None or not verify_password(password, user.password_hash):
            raise UnauthorizedException("Invalid phone number or password")

        if user.role is None or user.role.name != RoleName.ADMIN.value:
            raise ForbiddenException(
                "Only ADMIN accounts can authenticate for Swagger access"
            )

        user.last_login_at = datetime.now(UTC)
        await self.audit.log(
            AuditAction.LOGIN,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        await self.session.refresh(user)

        return {
            "access_token": create_access_token(subject=str(user.id)),
            "token_type": "bearer",
        }

    async def request_otp(
        self,
        *,
        phone: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        normalized = normalize_phone_number(phone)
        user = await self.users.get_by_phone(normalized)
        if user is None:
            raise NotFoundException(
                "Account not found for this phone number. Please register first."
            )
        if not user.is_active:
            raise UnauthorizedException("Account is not active")

        now = datetime.now(UTC)
        hour_ago = now - timedelta(hours=1)
        recent = (
            await self.session.execute(
                select(func.count(OtpCode.id)).where(
                    OtpCode.phone_number == normalized,
                    OtpCode.created_at >= hour_ago,
                )
            )
        ).scalar_one()
        if recent >= settings.otp_max_per_phone_per_hour:
            raise BadRequestException(
                "Too many OTP requests for this number. Please try again later."
            )

        await self.session.execute(
            update(OtpCode)
            .where(
                OtpCode.phone_number == normalized,
                OtpCode.verified_at.is_(None),
                OtpCode.expires_at > now,
            )
            .values(expires_at=now)
        )

        code = self._generate_otp_code()
        expires_at = now + timedelta(seconds=settings.otp_expire_seconds)
        otp = OtpCode(
            phone_number=normalized,
            user_id=user.id,
            code_hash=self._hash_otp(normalized, code),
            purpose="LOGIN",
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self.session.add(otp)
        await self.audit.log(
            AuditAction.OTP_REQUEST,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            details={"phone": self._mask_phone(normalized)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        await send_otp(normalized, code, round(settings.otp_expire_seconds / 60))
        return {
            "dev_otp": code if is_dev_sms() else None,
            "expires_in": settings.otp_expire_seconds,
        }

    async def verify_otp_login(
        self,
        *,
        phone: str,
        code: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        normalized = normalize_phone_number(phone)
        now = datetime.now(UTC)
        result = await self.session.execute(
            select(OtpCode)
            .where(
                OtpCode.phone_number == normalized,
                OtpCode.verified_at.is_(None),
            )
            .order_by(OtpCode.created_at.desc())
            .limit(1)
        )
        otp = result.scalar_one_or_none()
        if otp is None:
            raise UnauthorizedException("Invalid or expired OTP")

        expected_hash = self._hash_otp(normalized, code)
        if not hmac_module.compare_digest(expected_hash, otp.code_hash):
            otp.attempts += 1
            if otp.attempts >= settings.otp_max_attempts:
                otp.expires_at = now
            await self.audit.log(
                AuditAction.OTP_FAILED,
                user_id=otp.user_id,
                entity_type="otp_code",
                entity_id=otp.id,
                details={
                    "phone": self._mask_phone(normalized),
                    "attempt": otp.attempts,
                },
                ip_address=ip_address,
                user_agent=user_agent,
            )
            await self.session.commit()
            raise UnauthorizedException("Invalid OTP")

        if otp.expires_at <= now:
            raise UnauthorizedException("OTP has expired. Please request a new one.")

        user = await self.users.get_by_id(otp.user_id) if otp.user_id else None
        if user is None or not user.is_active:
            raise UnauthorizedException("Account not found or inactive")

        otp.verified_at = now
        user.is_phone_verified = True
        user.last_login_at = now
        await self.audit.log(
            AuditAction.OTP_VERIFY,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            details={"phone": self._mask_phone(normalized)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        await self.session.refresh(user)
        return await self._issue_tokens(user, ip_address, user_agent)

    @staticmethod
    def _generate_otp_code() -> str:
        return f"{secrets.randbelow(10 ** settings.otp_length):0{settings.otp_length}d}"

    @staticmethod
    def _hash_otp(phone: str, code: str) -> str:
        return hashlib.sha256(
            f"{phone}:{code}:{settings.jwt_secret}".encode()
        ).hexdigest()

    @staticmethod
    def _mask_phone(phone: str) -> str:
        return f"{phone[:5]}****{phone[-2:]}"

    async def refresh(
        self,
        *,
        refresh_token: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        payload = decode_refresh_token(refresh_token)
        if payload is None:
            raise UnauthorizedException("Invalid or expired refresh token")

        token_hash = hash_refresh_token(refresh_token)
        result = await self.session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        stored = result.scalar_one_or_none()
        if (
            stored is None
            or stored.revoked_at is not None
            or stored.expires_at <= datetime.now(UTC)
        ):
            raise UnauthorizedException("Refresh token has been revoked or expired")

        user = await self.users.get_by_id(stored.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedException("User account is not active")

        stored.revoked_at = datetime.now(UTC)
        await self.session.flush()
        return await self._issue_tokens(user, ip_address, user_agent)

    async def logout(self, refresh_token: str, user_id: UUID | None = None) -> None:
        token_hash = hash_refresh_token(refresh_token)
        result = await self.session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        stored = result.scalar_one_or_none()
        if stored and stored.revoked_at is None:
            stored.revoked_at = datetime.now(UTC)
        await self.audit.log(
            AuditAction.LOGOUT, user_id=user_id, ip_address=None, user_agent=None
        )
        await self.session.flush()

    async def change_password(
        self,
        user: User,
        *,
        current_password: str,
        new_password: str,
    ) -> None:
        if not verify_password(current_password, user.password_hash):
            raise BadRequestException("Current password is incorrect")
        user.password_hash = hash_password(new_password)
        await self.audit.log(
            AuditAction.PASSWORD_CHANGE,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
        )
        await self.session.flush()

    async def forgot_password(
        self,
        *,
        phone_number: str,
        new_password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> User:
        """Reset a user's password by phone number.

        DEV/TEST ONLY: this endpoint performs NO ownership verification
        (no OTP, SMS, email, or token). Anyone who knows another user's
        phone number could change that user's password. It is intentionally
        unsecured for development/testing and is NOT suitable for production.
        """
        normalized = normalize_phone_number(phone_number)
        user = await self.users.get_by_phone(normalized)
        if user is None:
            raise NotFoundException("User")

        user.password_hash = hash_password(new_password)
        await self.audit.log(
            AuditAction.PASSWORD_CHANGE,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            details={"phone": self._mask_phone(normalized)},
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        return user

    async def _issue_tokens(
        self,
        user: User,
        ip_address: str | None,
        user_agent: str | None,
    ) -> dict:
        access_token = create_access_token(subject=str(user.id))
        refresh_token = create_refresh_token(subject=str(user.id))

        payload = decode_refresh_token(refresh_token)
        expires_at = (
            datetime.fromtimestamp(payload["exp"], tz=UTC)
            if payload
            else datetime.now(UTC)
        )

        await self.users.create_refresh_token(
            user_id=user.id,
            token_hash=hash_refresh_token(refresh_token),
            expires_at=expires_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self.session.flush()
        await self.session.refresh(user)

        return {
            "user": user,
            "tokens": {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "bearer",
                "expires_in": settings.access_token_expire_minutes * 60,
            },
        }
