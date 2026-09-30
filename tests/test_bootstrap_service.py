from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.constants import RoleName
from app.core.permissions import ROLE_PERMISSIONS
from app.database import async_session_factory, engine
from app.models.user import Permission as PermissionModel
from app.models.user import RolePermission
from app.services.bootstrap_service import (
    _collect_permissions,
    _seed_permissions,
    _seed_roles,
)

# Every test here shares the module-level engine's loop, the same way
# test_schema_guard.py does.
pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _fresh_pool():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_seeded_permissions_match_the_permission_enum():
    async with async_session_factory() as session:
        models = await _seed_permissions(session)
        await session.commit()

    assert set(models) == set(_collect_permissions())
    assert len(models) == 43

    async with async_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(PermissionModel))
    assert count == len(models), "seeding must not create duplicate permissions"


async def test_seeded_roles_get_exactly_the_declared_grants():
    async with async_session_factory() as session:
        permissions = await _seed_permissions(session)
        roles = await _seed_roles(session, permissions)
        await session.commit()

        stored = {
            (role_name, perm_name)
            for role_name, role in roles.items()
            for perm_name, perm in permissions.items()
            if (
                await session.execute(
                    select(
                        RolePermission.permission_id
                    ).where(
                        RolePermission.role_id == role.id,
                        RolePermission.permission_id == perm.id,
                    )
                )
            ).scalar_one_or_none()
            is not None
        }

    expected = {
        (role_name, perm_name)
        for role_name, granted in ROLE_PERMISSIONS.items()
        for perm_name in granted
        if perm_name in permissions
    }
    assert stored == expected


async def test_seeding_is_idempotent():
    """A second run must not duplicate grants.

    The startup path runs on every cold start, so re-seeding has to converge
    rather than accumulate rows.
    """
    async with async_session_factory() as session:
        permissions = await _seed_permissions(session)
        roles = await _seed_roles(session, permissions)
        await session.commit()
        before = await session.scalar(
            select(func.count()).select_from(RolePermission)
        )

    async with async_session_factory() as session:
        permissions = await _seed_permissions(session)
        roles = await _seed_roles(session, permissions)
        await session.commit()
        after = await session.scalar(select(func.count()).select_from(RolePermission))

    assert after == before, "re-seeding must be a no-op"
    assert set(roles) == {r.value for r in RoleName}


async def test_seeding_uses_a_bounded_number_of_queries():
    """Guard the N+1 that pushed startup past the platform's 60s limit.

    One query per permission, and one per (role, permission) pair, meant ~250
    round trips to the hosted database. The bulk versions must stay constant.
    """
    from sqlalchemy import event

    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    try:
        async with async_session_factory() as session:
            permissions = await _seed_permissions(session)
            await _seed_roles(session, permissions)
            await session.commit()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # 6 roles + a handful of fetches, not 6 * len(permissions).
    assert len(selects) < 30, f"too many queries: {len(selects)}"
