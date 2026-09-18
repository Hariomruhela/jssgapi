from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.exceptions import (
    AlreadyExistsException,
    BadRequestException,
    NotFoundException,
)
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.security import hash_password
from app.dependencies import DBSession, require_permission
from app.models.user import Role, User
from app.repositories.user_repository import UserRepository
from app.schemas.user import RoleOut, UserCreate, UserOut, UserUpdate
from app.utils.validators import normalize_phone_number

router = APIRouter(prefix="/users", tags=["Users"])

READ = Depends(require_permission(Permission.USER_READ))
MANAGE = Depends(require_permission(Permission.USER_MANAGE))


async def _resolve_role(db, role_name: str | None) -> UUID | None:
    if role_name is None:
        return None
    result = await db.execute(select(Role).where(Role.name == role_name))
    role = result.scalar_one_or_none()
    if role is None:
        raise BadRequestException(f"Role '{role_name}' does not exist")
    return role.id


@router.get("", dependencies=[READ])
async def list_users(
    db: DBSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = UserRepository(db)
    users = await repo.list_all(limit=page_size, offset=(page - 1) * page_size)
    total = (await db.execute(select(func.count(User.id)))).scalar_one()
    return paginated(
        "Users fetched successfully",
        [UserOut.model_validate(u) for u in users],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("", dependencies=[MANAGE])
async def create_user(body: UserCreate, db: DBSession):
    repo = UserRepository(db)
    if await repo.get_by_email(body.email):
        raise AlreadyExistsException("User with this email already exists")
    role_id = await _resolve_role(db, body.role_name)
    user = await repo.create(
        email=body.email,
        full_name=body.full_name,
        phone_number=(
            normalize_phone_number(body.phone_number) if body.phone_number else None
        ),
        password_hash=hash_password(body.password),
        is_active=body.is_active,
        role_id=role_id,
    )
    await db.flush()
    return created("User created successfully", UserOut.model_validate(user))


@router.get("/roles", dependencies=[READ])
async def list_roles(db: DBSession):
    result = await db.execute(select(Role).order_by(Role.name))
    roles = [RoleOut.model_validate(r) for r in result.scalars().all()]
    return ok("Roles fetched successfully", roles)


@router.get("/{user_id}", dependencies=[READ])
async def get_user(user_id: UUID, db: DBSession):
    user = await UserRepository(db).get_by_id(user_id)
    if user is None:
        raise NotFoundException("User", str(user_id))
    return ok("User fetched successfully", UserOut.model_validate(user))


@router.patch("/{user_id}", dependencies=[MANAGE])
async def update_user(user_id: UUID, body: UserUpdate, db: DBSession):
    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)
    if user is None:
        raise NotFoundException("User", str(user_id))
    data = body.model_dump(exclude_unset=True)
    role_name = data.pop("role_name", None)
    if role_name:
        data["role_id"] = await _resolve_role(db, role_name)
    user = await repo.update(user, **data)
    return ok("User updated successfully", UserOut.model_validate(user))


@router.delete("/{user_id}", dependencies=[MANAGE])
async def delete_user(user_id: UUID, db: DBSession):
    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)
    if user is None:
        raise NotFoundException("User", str(user_id))
    await repo.delete(user)
    return ok("User deleted successfully")
