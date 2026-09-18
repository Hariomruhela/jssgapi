from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.exceptions import BadRequestException
from app.core.responses import ok
from app.dependencies import CurrentUser, DBSession
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    OtpRequest,
    OtpVerifyRequest,
    RefreshRequest,
    RegisterRequest,
)
from app.schemas.user import UserOut
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Auth"])


def _request_context(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    return ip, ua


@router.post("/register", response_model=AuthResponse)
async def register(body: RegisterRequest, request: Request, db: DBSession):
    ip, ua = _request_context(request)
    service = AuthService(db)
    result = await service.register(
        full_name=body.full_name,
        email=body.email,
        password=body.password,
        phone_number=body.phone_number,
        ip_address=ip,
        user_agent=ua,
    )
    return AuthResponse(
        user=UserOut.model_validate(result["user"]),
        tokens=result["tokens"],
    )


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest, request: Request, db: DBSession):
    ip, ua = _request_context(request)
    service = AuthService(db)
    result = await service.login(
        email=body.email, password=body.password, ip_address=ip, user_agent=ua
    )
    return AuthResponse(
        user=UserOut.model_validate(result["user"]),
        tokens=result["tokens"],
    )


@router.post("/otp/request")
async def request_otp(body: OtpRequest, request: Request, db: DBSession):
    ip, ua = _request_context(request)
    result = await AuthService(db).request_otp(
        phone=body.phone, ip_address=ip, user_agent=ua
    )
    return ok("OTP sent successfully", result)


@router.post("/otp/verify", response_model=AuthResponse)
async def verify_otp(body: OtpVerifyRequest, request: Request, db: DBSession):
    ip, ua = _request_context(request)
    service = AuthService(db)
    result = await service.verify_otp_login(
        phone=body.phone, code=body.otp, ip_address=ip, user_agent=ua
    )
    return AuthResponse(
        user=UserOut.model_validate(result["user"]),
        tokens=result["tokens"],
    )


@router.post("/refresh", response_model=AuthResponse)
async def refresh_token(body: RefreshRequest, request: Request, db: DBSession):
    ip, ua = _request_context(request)
    service = AuthService(db)
    result = await service.refresh(
        refresh_token=body.refresh_token, ip_address=ip, user_agent=ua
    )
    return AuthResponse(
        user=UserOut.model_validate(result["user"]),
        tokens=result["tokens"],
    )


@router.post("/logout")
async def logout(body: LogoutRequest, db: DBSession, current_user: CurrentUser):
    await AuthService(db).logout(body.refresh_token, user_id=current_user.id)
    return ok("Logged out successfully")


@router.get("/me", response_model=UserOut)
async def me(current_user: CurrentUser):
    return current_user


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    current_user: CurrentUser,
    db: DBSession,
):
    if body.current_password == body.new_password:
        raise BadRequestException("New password must differ from the current password")
    await AuthService(db).change_password(
        current_user,
        current_password=body.current_password,
        new_password=body.new_password,
    )
    return ok("Password changed successfully")
