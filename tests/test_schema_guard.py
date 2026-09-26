from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import async_session_factory, engine
from app.services import schema_guard

# The module-level engine's connection pool is bound to one event loop, so every
# test here must share a loop scope (test_profile.py does the same via a
# module-scoped TestClient).
pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _fresh_pool():
    """Empty the connection pool around each test.

    Other test modules drive the same global engine from a different event
    loop; without this, a pooled connection created under one loop is reused
    under another and asyncpg raises "attached to a different loop".
    """
    await engine.dispose()
    yield
    await engine.dispose()

# Covers all three repair paths: nullable, NOT NULL with a server default, and
# NOT NULL without one.
RESTORE_DDL = {
    "blood_group": (
        'ALTER TABLE members ADD COLUMN IF NOT EXISTS "blood_group" VARCHAR(10)'
    ),
    "profile_data": (
        "ALTER TABLE members ADD COLUMN IF NOT EXISTS profile_data JSONB "
        "DEFAULT '{}'::jsonb NOT NULL"
    ),
    "is_profile_complete": (
        "ALTER TABLE members ADD COLUMN IF NOT EXISTS is_profile_complete "
        "BOOLEAN DEFAULT false NOT NULL"
    ),
}
COLUMNS = tuple(RESTORE_DDL)


def _sync_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


def _exec(sql: str) -> None:
    engine = create_engine(_sync_url(), poolclass=NullPool)
    try:
        with engine.begin() as connection:
            connection.execute(text(sql))
    finally:
        engine.dispose()


def _query_one(sql: str, **params):
    engine = create_engine(_sync_url(), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return connection.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def _column_exists(column: str) -> bool:
    return bool(
        _query_one(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='members' "
            "AND column_name=:column)",
            column=column,
        )
    )


@pytest.fixture
def drifted_columns():
    """Drop real members columns, restoring them with raw DDL on teardown.

    Restoration deliberately avoids the code under test so a failure inside the
    guard cannot leave the database broken for later tests.
    """
    dropped = [c for c in COLUMNS if _column_exists(c)]
    assert len(dropped) == len(COLUMNS), f"missing setup columns: {dropped}"
    for column in dropped:
        _exec(f'ALTER TABLE members DROP COLUMN "{column}"')
    try:
        yield dropped
    finally:
        for column in dropped:
            _exec(RESTORE_DDL[column])
        still_missing = [c for c in dropped if not _column_exists(c)]
        assert not still_missing, f"cleanup failed: {still_missing}"


async def test_healthy_schema_reports_no_missing_columns():
    async with async_session_factory() as session:
        assert await schema_guard.find_missing_columns(session) == {}
        assert await schema_guard.ensure_schema(session) == []


async def test_guard_detects_every_dropped_column(drifted_columns):
    async with async_session_factory() as session:
        missing = await schema_guard.find_missing_columns(session)
    assert sorted(missing.get("members", [])) == sorted(drifted_columns)


async def test_guard_restores_every_dropped_column(drifted_columns):
    async with async_session_factory() as session:
        repaired = await schema_guard.ensure_schema(session)
    assert sorted(repaired) == sorted(f"members.{c}" for c in drifted_columns)
    for column in drifted_columns:
        assert _column_exists(column), f"{column} was not restored"


async def test_guard_backfills_not_null_columns_on_a_populated_table(
    drifted_columns,
):
    async with async_session_factory() as session:
        await schema_guard.ensure_schema(session)

    if not _query_one("SELECT EXISTS (SELECT 1 FROM members)"):
        pytest.skip("members is empty, nothing to backfill")

    assert not _query_one(
        "SELECT EXISTS (SELECT 1 FROM members WHERE profile_data IS NULL)"
    )
    assert not _query_one(
        "SELECT EXISTS (SELECT 1 FROM members WHERE is_profile_complete IS NULL)"
    )


async def test_guard_enforces_not_null_on_restored_columns(drifted_columns):
    async with async_session_factory() as session:
        await schema_guard.ensure_schema(session)

    with pytest.raises(Exception, match="null value"):
        _exec(
            "INSERT INTO members (id, user_id, first_name, last_name, "
            "membership_status, is_profile_complete, is_deleted) VALUES "
            "(gen_random_uuid(), gen_random_uuid(), 'x', 'y', 'pending', "
            "NULL, false)"
        )


async def test_guard_never_raises_when_a_column_cannot_be_added(
    drifted_columns, monkeypatch: pytest.MonkeyPatch
):
    async def boom(*args, **kwargs):
        raise RuntimeError("permission denied for table members")

    monkeypatch.setattr(schema_guard, "_add_column", boom)
    async with async_session_factory() as session:
        assert await schema_guard.ensure_schema(session) == []

    for column in drifted_columns:
        assert not _column_exists(column), f"{column} should not have been added"


async def test_guard_survives_an_unreadable_schema(monkeypatch: pytest.MonkeyPatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(schema_guard, "find_missing_columns", boom)
    monkeypatch.setattr(schema_guard, "find_missing_tables", boom)
    async with async_session_factory() as session:
        assert await schema_guard.ensure_schema(session) == []
        assert await schema_guard.report_missing_tables(session) == []


async def test_guard_is_idempotent_across_repeated_runs(drifted_columns):
    async with async_session_factory() as session:
        await schema_guard.ensure_schema(session)
    async with async_session_factory() as session:
        assert await schema_guard.ensure_schema(session) == []


async def test_find_missing_tables_ignores_alembic_version():
    async with async_session_factory() as session:
        assert "alembic_version" not in await schema_guard.find_missing_tables(session)


@pytest.mark.parametrize(
    ("pg_type", "expected"),
    [
        ("JSONB", "'{}'::jsonb"),
        ("BOOLEAN", "false"),
        ("UUID", "gen_random_uuid()"),
        ("TIMESTAMP WITH TIME ZONE", "now()"),
        ("INTEGER", "0"),
        ("NUMERIC(10, 2)", "0"),
        ("VARCHAR(100)", "''"),
        ("TEXT", "''"),
    ],
)
async def test_sentinel_literals(pg_type: str, expected: str):
    assert schema_guard._sentinel_literal(pg_type) == expected
