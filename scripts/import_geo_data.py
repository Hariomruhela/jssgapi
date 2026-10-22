"""Import India State/UT, district and city master data from ``data/geo/``.

Reads the checked-in LGD-sourced seed files (see ``data/geo/MANIFEST.md`` for
provenance) and upserts them into ``geo_states``, ``geo_districts`` and
``geo_cities``. Group-ID prefixes are never rewritten for cities that already
exist in the database, so already-issued Group IDs keep meaning the same city.

Run from the project root:

    # validate the files and report what would change, write nothing
    .venv/bin/python -m scripts.import_geo_data --dry-run

    # import / update the master data
    .venv/bin/python -m scripts.import_geo_data --apply

The import is idempotent: every row is keyed by its official LGD code, so
re-running it updates descriptive columns in place and creates nothing new.
Requires the ``e7f8a9b0c1d2`` migration to have been applied
(``.venv/bin/alembic upgrade head``).
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.database import async_session_factory, engine
from app.services.geo_service import (
    DATA_DIR,
    import_geo_data,
    validate_geo_files,
)


async def _run(apply_changes: bool) -> int:
    # Validate the files first so a dry run and a real run fail identically.
    counts = validate_geo_files(DATA_DIR)

    report: dict = {
        "data_dir": str(DATA_DIR),
        **counts,
    }

    if apply_changes:
        async with async_session_factory() as session:
            summary = await import_geo_data(session)
            await session.commit()
        report["states"] = summary.states_upserted
        report["districts"] = summary.districts_upserted
        report["cities"] = summary.cities_upserted
        report["cities_new"] = summary.cities_created
    else:
        report["dry_run"] = True

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not apply_changes:
        print("Dry run: files validated, nothing was written. Re-run with --apply.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.import_geo_data",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the seed files and report without writing (default).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Upsert states, districts and cities into the database.",
    )
    return parser.parse_args(argv)


async def main(args: argparse.Namespace) -> int:
    try:
        return await _run(apply_changes=args.apply)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
