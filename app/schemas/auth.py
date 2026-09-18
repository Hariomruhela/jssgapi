from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.user import UserOut
from app.utils.validators import (
    normalize_phone_number,
    validate_email,
    validate_password,
    validate_phone,
)


class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=255)
    email: str
    password: str = Field(min_length=8, max_length=128)
    phone_number: str | None = None

    def model_post_init(self, __context) -> None:
        if not validate_email(self.email):
            raise ValueError("Invalid email address")
        if self.phone_number and not validate_phone(self.phone_number):
            raise ValueError("Invalid phone number")
        validate_password(self.password)


class LoginRequest(BaseModel):
    email: str
    password: str


class OtpRequest(BaseModel):
    phone: str

    def model_post_init(self, __context) -> None:
        self.phone = normalize_phone_number(self.phone)


class OtpVerifyRequest(BaseModel):
    phone: str
    otp: str

    def model_post_init(self, __context) -> None:
        self.phone = normalize_phone_number(self.phone)
        if not self.otp.isdigit() or len(self.otp) != 6:
            raise ValueError("OTP must be a 6-digit number")


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)

    def model_post_init(self, __context) -> None:
        validate_password(self.new_password)


class AuthToken(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user: UserOut
    tokens: AuthToken
