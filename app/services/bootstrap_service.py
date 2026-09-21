from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.core.constants import RoleName
from app.core.permissions import ROLE_PERMISSIONS, Permission
from app.core.security import hash_password
from app.models.user import Permission as PermissionModel
from app.models.user import Role, RolePermission, User

logger = logging.getLogger(__name__)


def _collect_permissions() -> dict[str, str]:
    perms: dict[str, str] = {}
    for attr in dir(Permission):
        if attr.startswith("_"):
            continue
        value = getattr(Permission, attr)
        if isinstance(value, str):
            perms[value] = value
    return perms


async def _seed_permissions(session: AsyncSession) -> dict[str, PermissionModel]:
    permission_models: dict[str, PermissionModel] = {}
    for name in _collect_permissions():
        perm = await PermissionModel.get_or_create(session, name)
        permission_models[name] = perm
    return permission_models


async def _seed_roles(
    session: AsyncSession, permission_models: dict[str, PermissionModel]
) -> dict[str, Role]:
    role_models: dict[str, Role] = {}
    for role_name in [r.value for r in RoleName]:
        description = _role_descriptions().get(role_name)
        role = await Role.get_or_create(session, RoleName(role_name), description)
        role_models[role_name] = role

        for perm_name in ROLE_PERMISSIONS.get(RoleName(role_name), set()):
            if perm_name in permission_models:
                exists = await session.execute(
                    select(RolePermission).where(
                        RolePermission.role_id == role.id,
                        RolePermission.permission_id == permission_models[perm_name].id,
                    )
                )
                if exists.scalar_one_or_none() is None:
                    session.add(
                        RolePermission(
                            role_id=role.id,
                            permission_id=permission_models[perm_name].id,
                        )
                    )
    return role_models


async def _create_default_super_admin(
    session: AsyncSession, role_models: dict[str, Role]
) -> None:
    settings = get_settings()
    admin_email = settings.app_env != "production" and "superadmin@jssg.local"
    if not admin_email:
        return
    existing = await session.execute(select(User).where(User.email == admin_email))
    if existing.scalar_one_or_none() is not None:
        return
    user = User(
        email=admin_email,
        phone_number="+910000000000",
        full_name="Super Admin",
        password_hash=hash_password("ChangeMe123!"),
        is_active=True,
        is_email_verified=True,
        role_id=role_models[RoleName.SUPER_ADMIN.value].id,
    )
    session.add(user)
    logger.warning(
        "Created default super admin %s. Change its password immediately.", admin_email
    )


async def bootstrap_app(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        try:
            permission_models = await _seed_permissions(session)
            role_models = await _seed_roles(session, permission_models)
            await _create_default_super_admin(session, role_models)
            await session.commit()
            logger.info(
                "Bootstrap complete: roles, permissions and default admin seeded."
            )
        except Exception:
            await session.rollback()
            logger.exception("Bootstrap failed")


def _role_descriptions() -> dict[str, str]:
    return {
        RoleName.SUPER_ADMIN.value: "Super administrator with full platform access",
        RoleName.FEDERATION_ADMIN.value: "Administrator for the entire federation",
        RoleName.ADMIN.value: "Platform administrator with federation-level access",
        RoleName.REGIONAL_ADMIN.value: "Administrator for a region",
        RoleName.GROUP_ADMIN.value: "Administrator for a social group",
        RoleName.MEMBER.value: "Registered community member",
    }
