"""Compare SQLAlchemy model metadata against the live Postgres schema.

Reports, for every mapped table:
  * missing tables
  * missing columns (the cause of ``UndefinedColumnError`` at runtime)
  * type mismatches
  * NOT NULL mismatches
  * columns present in the DB but absent from the model
  * missing indexes / unique constraints

Usage:
    python -m scripts.drift_check                 # report only
    python -m scripts.drift_check --sql           # also print fixable DDL
    DATABASE_URL=postgresql://... python -m scripts.drift_check

The target database comes from DATABASE_URL (falls back to .env), so the same
command works against local, staging and production.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from typing import Any

from sqlalchemy import UniqueConstraint, create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine

from app.config import get_settings
from app.models import Base  # noqa: F401  (importing registers all mappers)

SYNC_PREFIX = "postgresql+psycopg2://"
ASYNC_PREFIXES = ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://")


def sync_database_url() -> str:
    url = get_settings().database_url
    for prefix in ASYNC_PREFIXES:
        if url.startswith(prefix):
            return SYNC_PREFIX + url[len(prefix) :]
    return url


def _sync_url_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _expected_type(column: Any) -> str:
    try:
        return column.type.compile(dialect=inspect(engine).bind.dialect)
    except Exception:
        return str(column.type)


def _default_text(column: Any) -> str:
    if column.server_default is None:
        return ""
    try:
        arg = column.server_default.arg
    except AttributeError:
        return str(column.server_default)
    return " ".join(str(arg).split())


def _db_default(row: Any) -> str:
    return (row.get("default") or "").strip().lower() or ""


_EQUIVALENT_TYPES = (
    # unconstrained FLOAT is double precision in Postgres - not drift
    ("FLOAT", "DOUBLE PRECISION"),
)


def _types_match(model_type: str, db_type: str) -> bool:
    if model_type == db_type:
        return True
    return any(
        {model_type, db_type} == set(pair) for pair in _EQUIVALENT_TYPES
    )


def _normalise_type(value: str) -> str:
    """Collapse whitespace and the two timestamp spellings to one canonical form."""
    text = " ".join(str(value).split()).upper()
    for variant in (
        "TIMESTAMP WITHOUT TIME ZONE",
        "TIMESTAMP WITH TIME ZONE",
    ):
        text = text.replace(variant, "TIMESTAMP")
    return text


def _index_names(index: Any) -> set[str]:
    return {
        name
        for name in index.get("column_names", []) or []
        if name is not None
    }


def _index_matches(expected: Any, actual: Any) -> bool:
    expected_cols = {column.name for column in expected.columns}
    return expected_cols == _index_names(actual)


def collect_drift(connection: Connection) -> dict[str, Any]:
    inspector = inspect(connection)
    db_tables = set(inspector.get_table_names(schema="public")) - {"alembic_version"}
    model_tables = set(Base.metadata.tables)

    missing_tables = sorted(model_tables - db_tables)
    extra_tables = sorted(db_tables - model_tables)

    missing_columns: dict[str, list[str]] = defaultdict(list)
    extra_columns: dict[str, list[str]] = defaultdict(list)
    type_mismatch: dict[str, list[str]] = defaultdict(list)
    nullability_mismatch: dict[str, list[str]] = defaultdict(list)
    missing_indexes: dict[str, list[str]] = defaultdict(list)
    missing_unique: dict[str, list[str]] = defaultdict(list)
    missing_fks: dict[str, list[str]] = defaultdict(list)

    for table_name in sorted(model_tables & db_tables):
        table = Base.metadata.tables[table_name]
        db_columns: dict[str, Any] = {
            col["name"]: col
            for col in inspector.get_columns(table_name, schema="public")
        }
        model_column_names = {column.name for column in table.columns}

        for column in table.columns:
            actual = db_columns.get(column.name)
            if actual is None:
                missing_columns[table_name].append(column.name)
                continue

            expected_type = _expected_type(column)
            actual_type = _normalise_type(str(actual["type"]))
            if not _types_match(
                _normalise_type(expected_type), actual_type
            ):
                type_mismatch[table_name].append(
                    f"{column.name} (model={expected_type} db={actual_type})"
                )

            model_not_null = not column.nullable
            db_not_null = not actual.get("nullable", True)
            if model_not_null != db_not_null:
                nullability_mismatch[table_name].append(
                    f"{column.name} (model_null={column.nullable} "
                    f"db_null={actual.get('nullable')})"
                )

            model_default = _normalise_type(_default_text(column))
            db_default = _normalise_type(_db_default(actual))
            if model_default and db_default and model_default != db_default:
                nullability_mismatch[table_name].append(
                    f"{column.name} (model_default={_default_text(column)!r} "
                    f"db_default={_db_default(actual)!r})"
                )

        for db_column in db_columns.values():
            if db_column["name"] not in model_column_names:
                extra_columns[table_name].append(db_column["name"])

        actual_indexes = inspector.get_indexes(table_name, schema="public")
        actual_uniques = {
            index["name"]: index
            for index in inspector.get_indexes(table_name, schema="public")
            if index.get("unique")
        }
        for index in table.indexes:
            existing = next(
                (i for i in actual_indexes if i["name"] == index.name), None
            )
            if existing is None or not _index_matches(index, existing):
                missing_indexes[table_name].append(str(index.name))

        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            cols = {col.name for col in constraint.columns}
            match = next(
                (
                    idx
                    for idx in actual_uniques.values()
                    if _index_names(idx) == cols
                ),
                None,
            )
            if match is None:
                label = constraint.name
                missing_unique[table_name].append(
                    str(label) if label else str(sorted(cols))
                )

        db_fk_sigs = {
            (
                tuple(sorted(fk["constrained_columns"])),
                fk["referred_table"],
                tuple(sorted(fk["referred_columns"])),
            )
            for fk in inspector.get_foreign_keys(table_name, schema="public")
        }
        for fk in table.foreign_key_constraints:
            sig = (
                tuple(sorted(fk.column_keys)),
                fk.referred_table.name,
                tuple(sorted(element.column.name for element in fk.elements)),
            )
            if sig not in db_fk_sigs:
                missing_fks[table_name].append(
                    f"{','.join(fk.column_keys)} -> {fk.referred_table.name}"
                )

    return {
        "missing_tables": missing_tables,
        "extra_tables": extra_tables,
        "missing_columns": dict(missing_columns),
        "extra_columns": dict(extra_columns),
        "type_mismatch": dict(type_mismatch),
        "nullability_mismatch": dict(nullability_mismatch),
        "missing_indexes": dict(missing_indexes),
        "missing_unique": dict(missing_unique),
        "missing_fks": dict(missing_fks),
    }


def column_ddl(table_name: str, column: Any) -> str:
    """Render an idempotent ADD COLUMN statement for one model column."""
    try:
        compiled = column.type.compile(dialect=inspect(engine).bind.dialect)
    except Exception:
        compiled = str(column.type)
    parts = [
        f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS "{column.name}"',
        compiled,
    ]
    default = column.server_default
    if default is not None:
        try:
            parts.append(f"DEFAULT {default.arg.text}")
        except AttributeError:
            pass
    if not column.nullable:
        parts.append("NOT NULL")
    return " ".join(parts) + ";"


def alembic_state(connection: Connection) -> dict[str, Any]:
    has_version = connection.dialect.has_table(
        connection, "alembic_version", schema="public"
    )
    current: list[str] = []
    if has_version:
        current = list(
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars()
        )
    heads = _alembic_heads()
    return {
        "table_present": has_version,
        "current": list(current),
        "heads": heads,
        "up_to_date": bool(heads) and set(current) >= set(heads),
    }


def _alembic_heads() -> list[str]:
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config("alembic.ini")
        config.set_main_option("script_location", "migrations")
        script = ScriptDirectory.from_config(config)
        return list(script.get_heads())
    except Exception as exc:  # pragma: no cover - diagnostics only
        print(f"  ! could not read alembic heads: {_sync_url_error(exc)}")
        return []


BLOCKING_SECTIONS = (
    ("missing_tables", "MISSING TABLE", "table"),
    ("missing_columns", "MISSING COLUMN", "column"),
)

ADVISORY_SECTIONS = (
    ("type_mismatch", "TYPE MISMATCH", "detail"),
    ("nullability_mismatch", "NULLABILITY/DEFAULT MISMATCH", "detail"),
    ("missing_indexes", "MISSING INDEX", "index"),
    ("missing_unique", "MISSING UNIQUE CONSTRAINT", "constraint"),
    ("missing_fks", "MISSING FOREIGN KEY", "fk"),
    ("extra_columns", "EXTRA COLUMN (in db, not in model)", "column"),
    ("extra_tables", "EXTRA TABLE (in db, not in model)", "table"),
)


def _rows(drift: dict[str, Any], key: str) -> list[tuple[str, str]]:
    data = drift.get(key)
    if not data:
        return []
    if isinstance(data, dict):
        return [
            (table, item if isinstance(item, str) else str(item))
            for table, items in data.items()
            for item in items
        ]
    return [(table, "") for table in data]


def _print_sections(drift: dict[str, Any], sections: tuple) -> int:
    total = 0
    for key, title, _kind in sections:
        rows = _rows(drift, key)
        if not rows:
            continue
        total += len(rows)
        print(f"{title} ({len(rows)}):")
        for table, item in rows:
            print(f"  {table}.{item}" if item else f"  {table}")
        print()
    return total


def blocking_count(drift: dict[str, Any]) -> int:
    return sum(len(_rows(drift, key)) for key, _title, _kind in BLOCKING_SECTIONS)


def print_report(drift: dict[str, Any], state: dict[str, Any], url: str) -> None:
    host = url.split("@")[-1].split("/")[0] if "@" in url else "?"
    print(f"Database: {host}")
    print(f"alembic_version table: {'present' if state['table_present'] else 'ABSENT'}")
    print(f"alembic current: {state['current'] or '(none)'}")
    print(f"alembic heads:   {state['heads'] or '(unknown)'}")
    if not state["table_present"] or not state["up_to_date"]:
        print("  ! alembic chain is NOT in sync with the repo")
    print()

    print("=== BLOCKING (breaks queries at runtime) ===")
    blocking = _print_sections(drift, BLOCKING_SECTIONS)
    if not blocking:
        print("none\n")

    print("=== ADVISORY (indexes / types / nullability / extras) ===")
    advisory = _print_sections(drift, ADVISORY_SECTIONS)
    if not advisory:
        print("none\n")

    if blocking:
        print(f"RESULT: {blocking} blocking item(s) - app code WILL fail.")
    else:
        print("RESULT: schema is query-compatible with the models.")


def print_sql(engine: Engine, drift: dict[str, Any]) -> None:
    for table_name in drift["missing_tables"]:
        table = Base.metadata.tables[table_name]
        columns = ", ".join(
            f'"{c.name}" {c.type.compile(dialect=engine.dialect)}'
            for c in table.columns
        )
        print(f'CREATE TABLE IF NOT EXISTS "{table_name}" ({columns});')
    for table_name, columns in drift["missing_columns"].items():
        table = Base.metadata.tables[table_name]
        for column in table.columns:
            if column.name in columns:
                print(column_ddl(table_name, column))


engine: Engine


def main() -> int:
    global engine
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sql", action="store_true", help="print idempotent DDL that fixes the drift"
    )
    args = parser.parse_args()

    url = sync_database_url()
    engine = create_engine(url, poolclass=None)
    try:
        with engine.connect() as connection:
            drift = collect_drift(connection)
            state = alembic_state(connection)
    except Exception as exc:
        print(
            f"Could not connect to the database: {_sync_url_error(exc)}",
            file=sys.stderr,
        )
        return 2
    finally:
        engine.dispose()

    print_report(drift, state, url)
    if args.sql:
        print("\n-- suggested DDL (idempotent, safe to re-run)")
        if not blocking_count(drift):
            print("-- nothing missing: no DDL required")
        print_sql(engine, drift)
    return 1 if blocking_count(drift) else 0


if __name__ == "__main__":
    raise SystemExit(main())
