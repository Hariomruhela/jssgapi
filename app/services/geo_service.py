"""Import India administrative master data from the checked-in LGD seed files.

Reads ``data/geo/states.csv``, ``data/geo/districts.csv`` and
``data/geo/cities.csv`` (see ``data/geo/MANIFEST.md`` for provenance) and
upserts them into ``geo_states`` / ``geo_districts`` / ``geo_cities``.

Guarantees:

* **Idempotent** - every row is keyed by its official LGD code; re-running the
  import updates descriptive columns in place and never duplicates a row.
* **Prefix-stable** - the Group-ID ``city_code``/``prefix`` of a city that
  already exists in the database is *never* changed, so already-generated
  Group IDs keep meaning the same city. A city whose code was *corrected* is
  fixed by the Alembic migration, not by the import.
* **Collision-checked** - the whole file is validated before anything is
  written: duplicate LGD codes, duplicate ``(state, city_code)`` pairs and
  duplicate prefixes all abort the import with a clear error.
* **Atomic** - one transaction; a failure rolls everything back.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.city_codes import REQUIRED_CITY_CODES
from app.models.geo import GeoCity, GeoDistrict, GeoState

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "geo"


def _normalize(value: str) -> str:
    """Case-folded, whitespace-collapsed name used for city lookups."""
    return " ".join(value.split()).casefold()


@dataclass(frozen=True)
class ImportSummary:
    states_upserted: int = 0
    districts_upserted: int = 0
    cities_upserted: int = 0
    cities_created: int = 0
    prefixes_reserved: dict[str, int] = field(default_factory=dict)


def _read_states(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"{path} contains no states")
    seen: set[str] = set()
    for row in rows:
        code = row["code"].strip()
        if len(code) != 2 or not code.isalpha() or code.upper() != code:
            raise ValueError(f"Invalid state code {code!r} for {row['name']!r}")
        if code in seen:
            raise ValueError(f"Duplicate state code {code!r} in {path}")
        seen.add(code)
    return rows


def _read_districts(path: Path, state_lgd_codes: set[str]) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    seen: set[str] = set()
    for row in rows:
        lgd = row["lgd_code"].strip()
        if lgd in seen:
            raise ValueError(f"Duplicate district LGD code {lgd} in {path}")
        seen.add(lgd)
        if row["state_lgd_code"].strip() not in state_lgd_codes:
            raise ValueError(
                f"District {row['name']!r} references unknown state LGD code "
                f"{row['state_lgd_code']!r}"
            )
    return rows


def _read_cities(
    path: Path, state_lgd_codes: set[str]
) -> tuple[list[dict], dict[str, int]]:
    """Validate the whole city file before any database write happens.

    Returns the rows plus per-state counts of how many prefixes each state
    will occupy, so callers can log/report them.
    """
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    seen_lgd: set[str] = set()
    seen_prefix: set[str] = set()
    state_lgd_to_code: dict[str, str] = {}
    # state lgd -> (city_code -> row name) for (state, city_code) checks
    per_state_codes: dict[str, dict[str, str]] = {}

    state_rows = _read_states(path.parent / "states.csv")
    for s in state_rows:
        state_lgd_to_code[s["lgd_code"].strip()] = s["code"].strip()

    prefixes: dict[str, int] = {}
    for row in rows:
        lgd = row["lgd_code"].strip()
        if lgd in seen_lgd:
            raise ValueError(f"Duplicate city LGD code {lgd} in {path}")
        seen_lgd.add(lgd)

        state_lgd = row["state_lgd_code"].strip()
        if state_lgd not in state_lgd_codes:
            raise ValueError(
                f"City {row['name']!r} references unknown state LGD code {state_lgd!r}"
            )

        city_code = row["city_code"].strip()
        if not city_code.isdigit() or int(city_code) < 1:
            raise ValueError(
                f"City {row['name']!r} has invalid city_code {city_code!r}"
            )

        # The required mapping (``app.core.city_codes``) is authoritative: the
        # codes are assigned, never derived. A seed file that disagrees is
        # corrected here, so the database always holds the required code.
        required = REQUIRED_CITY_CODES.get(int(lgd))
        if required is not None and required != city_code:
            logger.warning(
                "City %s (LGD %s) has city_code %s in the seed file; the required "
                "mapping says %s. Using %s.",
                row["name"],
                lgd,
                city_code,
                required,
                required,
            )
            city_code = required
            row["city_code"] = city_code

        state_code = state_lgd_to_code[state_lgd]
        prefix = f"{state_code}{city_code}"
        if prefix in seen_prefix:
            raise ValueError(
                f"Prefix collision: {prefix} is claimed by more than one city "
                f"(one of them is {row['name']!r} in state LGD {state_lgd})"
            )
        seen_prefix.add(prefix)
        prefixes[prefix] = prefixes.get(prefix, 0) + 1

        taken = per_state_codes.setdefault(state_lgd, {})
        if city_code in taken:
            raise ValueError(
                f"City code {city_code} is used twice in state {state_code}: "
                f"{taken[city_code]!r} and {row['name']!r}"
            )
        taken[city_code] = row["name"]

    return rows, prefixes


def validate_geo_files(data_dir: Path = DATA_DIR) -> dict[str, int]:
    """Parse and validate every seed file without touching the database.

    Raises ``ValueError`` on any duplicate LGD code, bad state reference,
    invalid city code or Group-ID prefix collision. Returns file row counts
    for reporting.
    """
    state_rows = _read_states(data_dir / "states.csv")
    state_lgd_codes = {r["lgd_code"].strip() for r in state_rows}
    district_rows = _read_districts(data_dir / "districts.csv", state_lgd_codes)
    _, prefixes = _read_cities(data_dir / "cities.csv", state_lgd_codes)
    return {
        "states": len(state_rows),
        "districts": len(district_rows),
        "cities": len(prefixes),
    }


async def import_geo_data(
    session: AsyncSession, data_dir: Path = DATA_DIR
) -> ImportSummary:
    """Upsert states, districts and cities from the seed CSVs. Idempotent."""
    state_rows = _read_states(data_dir / "states.csv")
    state_lgd_codes = {r["lgd_code"].strip() for r in state_rows}
    district_rows = _read_districts(data_dir / "districts.csv", state_lgd_codes)
    city_rows, prefixes = _read_cities(data_dir / "cities.csv", state_lgd_codes)

    # --- states ------------------------------------------------------------
    existing_states = {
        s.lgd_code: s
        for s in (await session.execute(select(GeoState))).scalars().all()
    }
    states_by_lgd: dict[str, GeoState] = {}
    states_upserted = 0
    for row in state_rows:
        lgd = int(row["lgd_code"])
        name = row["name"].strip()
        code = row["code"].strip()
        is_ut = row["is_union_territory"].strip().lower() == "true"
        state = existing_states.get(lgd)
        if state is None:
            state = GeoState(lgd_code=lgd, name=name, code=code)
            session.add(state)
            states_upserted += 1
        else:
            # ``code`` is deliberately NOT updated: it is the Group-ID prefix
            # and must never drift out from under already-issued IDs.
            if state.name != name or state.is_union_territory != is_ut:
                state.name = name
                state.is_union_territory = is_ut
                states_upserted += 1
        state.is_union_territory = is_ut
        states_by_lgd[str(lgd)] = state
    await session.flush()

    # --- districts ---------------------------------------------------------
    existing_districts = {
        d.lgd_code: d
        for d in (await session.execute(select(GeoDistrict))).scalars().all()
    }
    districts_upserted = 0
    district_by_lgd: dict[str, GeoDistrict] = {}
    for row in district_rows:
        lgd = int(row["lgd_code"])
        name = row["name"].strip()
        parent = states_by_lgd[row["state_lgd_code"].strip()]
        district = existing_districts.get(lgd)
        if district is None:
            district = GeoDistrict(lgd_code=lgd, name=name, state_id=parent.id)
            session.add(district)
            districts_upserted += 1
        elif district.name != name or district.state_id != parent.id:
            district.name = name
            district.state_id = parent.id
            districts_upserted += 1
        district_by_lgd[str(lgd)] = district
    await session.flush()

    # --- cities ------------------------------------------------------------
    existing_cities = {
        c.lgd_code: c for c in (await session.execute(select(GeoCity))).scalars()
    }
    cities_upserted = 0
    cities_created = 0
    for row in city_rows:
        lgd = int(row["lgd_code"])
        name = row["name"].strip()
        parent = states_by_lgd[row["state_lgd_code"].strip()]
        district_lgd = row["district_lgd_code"].strip()
        district = district_by_lgd.get(district_lgd) if district_lgd else None
        ulb_raw = row.get("ulb_type_code", "").strip()
        census_raw = row.get("census_2011_code", "").strip()
        city_code = row["city_code"].strip()
        prefix = f"{parent.code}{city_code}"

        city = existing_cities.get(lgd)
        if city is None:
            city = GeoCity(
                lgd_code=lgd,
                name=name,
                name_norm=_normalize(name),
                state_id=parent.id,
                district_id=district.id if district else None,
                ulb_type_code=int(ulb_raw) if ulb_raw.isdigit() else None,
                census_2011_code=census_raw or None,
                city_code=city_code,
                prefix=prefix,
            )
            session.add(city)
            cities_created += 1
            cities_upserted += 1
        else:
            # ``city_code``/``prefix`` are never rewritten for an existing
            # city: generated Group IDs must keep pointing at the same city.
            if city.prefix != prefix:
                logger.warning(
                    "City %s (LGD %s) already has prefix %s; seed file says %s. "
                    "Keeping the existing prefix - run 'alembic upgrade head' to "
                    "apply a corrected code.",
                    name,
                    lgd,
                    city.prefix,
                    prefix,
                )
            changed = False
            if city.name != name:
                city.name = name
                city.name_norm = _normalize(name)
                changed = True
            new_district = district.id if district else None
            if city.district_id != new_district:
                city.district_id = new_district
                changed = True
            new_ulb = int(ulb_raw) if ulb_raw.isdigit() else None
            if city.ulb_type_code != new_ulb:
                city.ulb_type_code = new_ulb
                changed = True
            new_census = census_raw or None
            if city.census_2011_code != new_census:
                city.census_2011_code = new_census
                changed = True
            if changed:
                cities_upserted += 1

    await session.flush()
    summary = ImportSummary(
        states_upserted=states_upserted,
        districts_upserted=districts_upserted,
        cities_upserted=cities_upserted,
        cities_created=cities_created,
        prefixes_reserved=prefixes,
    )
    logger.info(
        "Geo import: %d states, %d districts, %d cities (%d new) upserted",
        summary.states_upserted,
        summary.districts_upserted,
        summary.cities_upserted,
        summary.cities_created,
    )
    return summary
