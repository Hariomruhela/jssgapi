from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.jwt import decode_access_token
from app.core.permissions import require_permission as check_permission
from app.core.security import bearer_scheme
from app.database import get_db
from app.models.user import User
from app.repositories.user_repository import UserRepository

settings = get_settings()


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    credentials: Annotated[str | None, Depends(bearer_scheme)] = None,
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    user_repo = UserRepository(db)
    user = await user_repo.get_by_id(UUID(user_id))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )
    return user


async def get_optional_user(
    db: AsyncSession = Depends(get_db),
    credentials: Annotated[str | None, Depends(bearer_scheme)] = None,
) -> User | None:
    if credentials is None:
        return None
    payload = decode_access_token(credentials)
    if payload is None:
        return None
    user_id = payload.get("sub")
    if user_id is None:
        return None
    try:
        user = await UserRepository(db).get_by_id(UUID(user_id))
    except ValueError:
        return None
    if user is None or not user.is_active:
        return None
    return user


def require_permission(permission: str):
    def _dependency(user: CurrentUser) -> User:
        check_permission(user, permission)
        return user

    return _dependency


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
DBSession = Annotated[AsyncSession, Depends(get_db)]
