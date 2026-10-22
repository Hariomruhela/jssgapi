from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.models import Base  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()


def _raw_database_url() -> str:
    """``DATABASE_URL`` exactly as configured, before the asyncpg rewrite.

    ``settings.database_url`` is normalised for asyncpg, which drops the
    libpq-only query params (``sslmode``, ``channel_binding``). Alembic runs
    on the sync psycopg2 driver, which needs ``sslmode`` back: hosted
    Postgres (Neon) refuses plaintext connections.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    try:
        from dotenv import dotenv_values

        root = Path(__file__).resolve().parents[1]
        return (dotenv_values(root / ".env").get("DATABASE_URL") or "").strip()
    except Exception:  # pragma: no cover - dotenv is optional at runtime
        return ""


def _sync_database_url() -> str:
    """The psycopg2 flavour of the configured database URL."""
    url = _raw_database_url() or settings.database_url
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme.startswith("postgres"):
        scheme = "postgresql+psycopg2"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in {"channel_binding", "pgbouncer"}
    ]
    return urlunsplit(parts._replace(scheme=scheme, query=urlencode(query)))


database_url = _sync_database_url()
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
