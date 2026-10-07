from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.core.constants import LEGACY_ROLE_ALIASES, RoleName
from app.core.permissions import ROLE_PERMISSIONS, Permission
from app.core.security import hash_password
from app.models.user import Permission as PermissionModel
from app.models.user import Role, RolePermission, User
from app.services.schema_guard import ensure_schema, report_missing_tables

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
    """Fetch every known permission, inserting only the ones that are missing.

    One SELECT replaces 42 round trips, which matters on a hosted database
    where each query costs real latency on the app's startup path.
    """
    wanted = _collect_permissions()
    rows = await session.execute(
        select(PermissionModel).where(PermissionModel.name.in_(list(wanted)))
    )
    permission_models: dict[str, PermissionModel] = {p.name: p for p in rows.scalars()}
    for name in wanted:
        if name not in permission_models:
            permission_models[name] = await PermissionModel.get_or_create(session, name)
    return permission_models


async def _seed_roles(
    session: AsyncSession, permission_models: dict[str, PermissionModel]
) -> dict[str, Role]:
    role_models: dict[str, Role] = {}
    for role_name in [r.value for r in RoleName]:
        description = _role_descriptions().get(role_name)
        role = await Role.get_or_create(session, RoleName(role_name), description)
        role_models[role_name] = role

    await _retire_legacy_roles(session, role_models)

    # Load the existing grants for every role in one query. Checking each
    # (role, permission) pair separately meant ~250 round trips, which pushed
    # startup past the 60s limit on a hosted Postgres.
    rows = await session.execute(
        select(RolePermission.role_id, RolePermission.permission_id).where(
            RolePermission.role_id.in_([role.id for role in role_models.values()])
        )
    )
    existing = {(role_id, perm_id) for role_id, perm_id in rows.all()}

    for role_name, role in role_models.items():
        for perm_name in ROLE_PERMISSIONS.get(RoleName(role_name), set()):
            perm = permission_models.get(perm_name)
            if perm is None:
                continue
            if (role.id, perm.id) in existing:
                continue
            existing.add((role.id, perm.id))
            session.add(
                RolePermission(
                    role_id=role.id,
                    permission_id=perm.id,
                )
            )
    return role_models


async def _retire_legacy_roles(
    session: AsyncSession, role_models: dict[str, Role]
) -> None:
    """Move accounts off the retired admin tiers and drop those role rows.

    Mirrors migration ``f2b3c4d5e6f7``. Vercel has no migration step, so this is
    what actually collapses the tiers on a deployed environment; running it here
    as well keeps a database that skipped the migration consistent with the
    authorization code.

    Safe to re-run: after the first pass no user references a retired role, so the
    UPDATE matches nothing and the row is already gone.
    """
    result = await session.execute(
        select(Role).where(Role.name.in_(list(LEGACY_ROLE_ALIASES)))
    )
    for role in result.scalars().all():
        survivor = role_models[LEGACY_ROLE_ALIASES[role.name]]
        moved = await session.execute(select(User.id).where(User.role_id == role.id))
        count = len(moved.scalars().all())
        await session.execute(
            update(User).where(User.role_id == role.id).values(role_id=survivor.id)
        )
        await session.delete(role)
        logger.warning(
            "Retired role %s: moved %d account(s) to %s",
            role.name,
            count,
            survivor.name,
        )


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
            # Reconcile the live schema before anything reads it. Vercel has no
            # migration step, so a deployed column can be missing in the
            # database; this repairs it instead of failing every request.
            await report_missing_tables(session)
            await ensure_schema(session)
        except Exception:
            await session.rollback()
            logger.exception("Schema guard failed")

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
        RoleName.GROUP_ADMIN.value: "Administrator for a single social group",
        RoleName.MEMBER.value: "Registered community member",
    }
