"""Self-healing schema guard.

Vercel serverless has no migration step, so a deployment that adds a column can
reach production before the DDL is applied. The result is a hard crash on every
request that touches the new column::

    asyncpg.exceptions.UndefinedColumnError: column members.profile_data does not exist

This module reconciles the live schema against the SQLAlchemy models on startup
and adds anything missing, so a drifted database repairs itself on the next
cold start instead of waiting for a manual migration run.

Design constraints:

* The column list is derived from the models at runtime, so the guard can never
  disagree with the ORM the way a hand-written migration can.
* One cheap ``information_schema`` query decides whether any DDL is needed; a
  healthy database pays a single extra query per cold start.
* Every statement is ``IF NOT EXISTS``, so concurrent cold starts are safe and
  re-runs are no-ops.
* Failures are logged, never raised. An app that cannot write DDL (insufficient
  grants) must still boot and serve traffic.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Base  # noqa: F401  (importing registers all mappers)

logger = logging.getLogger(__name__)

SCHEMA = "public"

_ALL_COLUMNS_SQL = text(
    "SELECT table_name, column_name "
    "FROM information_schema.columns "
    "WHERE table_schema = :schema"
)

_ALL_TABLES_SQL = text(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = :schema AND table_type = 'BASE TABLE'"
)


def _postgres_type(column: Any) -> str:
    return column.type.compile(dialect=postgresql.dialect())


def _server_default(column: Any) -> str | None:
    default = column.server_default
    if default is None:
        return None
    arg_text = getattr(getattr(default, "arg", None), "text", None)
    return arg_text if isinstance(arg_text, str) and arg_text else None


def _sentinel_literal(pg_type: str) -> str:
    """A value to backfill a NOT NULL column that has no server default."""
    normalised = pg_type.upper()
    if normalised == "JSONB":
        return "'{}'::jsonb"
    if normalised == "BOOLEAN":
        return "false"
    if normalised == "UUID":
        return "gen_random_uuid()"
    if normalised.startswith("TIMESTAMP"):
        return "now()"
    if any(
        token in normalised
        for token in ("INT", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL")
    ):
        return "0"
    return "''"


async def _table_has_rows(session: AsyncSession, table_name: str) -> bool:
    exists = (
        await session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name = :table)"
            ),
            {"schema": SCHEMA, "table": table_name},
        )
    ).scalar()
    if not exists:
        return False
    count = (
        await session.execute(
            text(f'SELECT EXISTS (SELECT 1 FROM "{table_name}" LIMIT 1)')  # noqa: S608
        )
    ).scalar()
    return bool(count)


async def find_missing_columns(session: AsyncSession) -> dict[str, list[str]]:
    """Model columns that exist in the ORM but not in the live database."""
    rows = (await session.execute(_ALL_COLUMNS_SQL, {"schema": SCHEMA})).all()
    live: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        live.setdefault(table_name, set()).add(column_name)

    missing: dict[str, list[str]] = {}
    for table_name, table in Base.metadata.tables.items():
        gap = sorted(
            column.name
            for column in table.columns
            if column.name not in live.get(table_name, set())
        )
        if gap:
            missing[table_name] = gap
    return missing


async def find_missing_tables(session: AsyncSession) -> list[str]:
    rows = (
        await session.execute(_ALL_TABLES_SQL, {"schema": SCHEMA})
    ).scalars().all()
    existing = set(rows)
    return sorted(set(Base.metadata.tables) - existing - {"alembic_version"})


async def _add_column(
    session: AsyncSession, table_name: str, column: Any
) -> None:
    pg_type = _postgres_type(column)
    quoted_table = f'"{table_name}"'
    quoted_column = f'"{column.name}"'
    default = _server_default(column)

    # Added nullable first: a NOT NULL column with no default cannot be added
    # to a populated table in a single statement.
    await session.execute(
        text(
            f"ALTER TABLE {quoted_table} "  # noqa: S608
            f"ADD COLUMN IF NOT EXISTS {quoted_column} {pg_type}"
        )
    )

    if not column.nullable and await _table_has_rows(session, table_name):
        # Existing rows are NULL after the ADD, so backfill before enforcing.
        backfill = default if default is not None else _sentinel_literal(pg_type)
        await session.execute(
            text(
                f"UPDATE {quoted_table} SET {quoted_column} = "  # noqa: S608
                f"{backfill} WHERE {quoted_column} IS NULL"
            )
        )

    if column.nullable:
        return

    await session.execute(
        text(
            f"ALTER TABLE {quoted_table} "  # noqa: S608
            f"ALTER COLUMN {quoted_column} SET NOT NULL"
        )
    )
    if default is not None:
        await session.execute(
            text(
                f"ALTER TABLE {quoted_table} "  # noqa: S608
                f"ALTER COLUMN {quoted_column} SET DEFAULT {default}"
            )
        )


async def ensure_schema(session: AsyncSession) -> list[str]:
    """Add any column the models declare but the database is missing.

    Returns the list of repaired ``table.column`` names. Never raises.
    """
    try:
        missing = await find_missing_columns(session)
    except Exception:
        logger.exception("Schema guard could not read the live schema")
        return []

    if not missing:
        logger.info("Schema guard: live schema matches the models.")
        return []

    repaired: list[str] = []
    for table_name, columns in missing.items():
        table = Base.metadata.tables[table_name]
        for column_name in columns:
            try:
                await _add_column(session, table_name, table.columns[column_name])
            except Exception:
                logger.exception(
                    "Schema guard failed to add %s.%s", table_name, column_name
                )
                await session.rollback()
                continue
            repaired.append(f"{table_name}.{column_name}")

    if repaired:
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Schema guard could not commit its DDL")
            return []
        logger.warning(
            "Schema guard repaired %d missing column(s): %s. "
            "Apply the Alembic migrations to keep the chain in sync.",
            len(repaired),
            ", ".join(repaired),
        )
    return repaired


async def report_missing_tables(session: AsyncSession) -> list[str]:
    """Log tables the models expect but the database lacks (not auto-created)."""
    try:
        missing = await find_missing_tables(session)
    except Exception:
        logger.exception("Schema guard could not list tables")
        return []
    if missing:
        logger.error(
            "Schema guard: %d table(s) missing and NOT auto-created (%s). "
            "Run `alembic upgrade head` against this database.",
            len(missing),
            ", ".join(missing),
        )
    return missing
