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

    async def register(
        self,
        *,
        full_name: str,
        email: str,
        password: str,
        phone_number: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        email = email.strip().lower()
        existing = await self.users.get_by_email(email)
        if existing:
            raise AlreadyExistsException("An account with this email already exists")
        phone_normalized = (
            normalize_phone_number(phone_number) if phone_number else None
        )
        if phone_normalized and await self.users.get_by_phone(phone_normalized):
            raise AlreadyExistsException("An account with this phone number exists")

        role_result = await self.session.execute(
            select(Role).where(Role.name == RoleName.MEMBER.value)
        )
        member_role = role_result.scalar_one_or_none()
        if member_role is None:
            raise BadRequestException("MEMBER role is not configured yet")

        user = await self.users.create(
            email=email,
            full_name=full_name,
            phone_number=phone_normalized,
            password_hash=hash_password(password),
            is_active=True,
            role_id=member_role.id,
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

    async def login(
        self,
        *,
        email: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        user = await self.users.get_active_by_email(email.strip().lower())
        if user is None or not verify_password(password, user.password_hash):
            raise UnauthorizedException("Invalid email or password")

        user.last_login_at = datetime.now(UTC)
        await self.session.flush()

        await self.audit.log(
            AuditAction.LOGIN,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return await self._issue_tokens(user, ip_address, user_agent)

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
