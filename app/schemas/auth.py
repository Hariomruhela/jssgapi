from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.user import UserOut
from app.utils.validators import (
    normalize_phone_number,
    validate_email,
    validate_password,
    validate_phone,
)


class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=255)
    phone_number: str
    email: str | None = Field(default=None, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)
    role: str | None = Field(default=None, max_length=50)
    msg91_token: str = Field(min_length=1, max_length=4096)

    @field_validator("msg91_token", mode="before")
    @classmethod
    def _normalize_msg91_token(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("msg91_token is required")
        return value

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        return value

    def model_post_init(self, __context) -> None:
        if not self.full_name.strip():
            raise ValueError("Full name cannot be empty")
        if not validate_phone(self.phone_number):
            raise ValueError("Invalid phone number")
        if self.email is not None and not validate_email(self.email):
            raise ValueError("Invalid email address")
        validate_password(self.password)
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")


class LoginRequest(BaseModel):
    email: str | None = None
    phone_number: str | None = None
    password: str

    def model_post_init(self, __context) -> None:
        if not self.email and not self.phone_number:
            raise ValueError("Either email or phone_number is required")
        if self.email and not validate_email(self.email):
            raise ValueError("Invalid email address")
        if self.phone_number and not validate_phone(self.phone_number):
            raise ValueError("Invalid phone number")


class ForgotPasswordRequest(BaseModel):
    phone_number: str
    new_password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    def model_post_init(self, __context) -> None:
        if not validate_phone(self.phone_number):
            raise ValueError("Invalid phone number")
        validate_password(self.new_password)
        if self.new_password != self.confirm_password:
            raise ValueError("Passwords do not match")


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


class OAuth2TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AuthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user: UserOut
    tokens: AuthToken
