"""Refresh ``data/geo/*.csv`` from the upstream LGD sources.

Re-downloads everything the seed files are built from and regenerates them:

* **states** - LGD state/UT list via the community mirror API
  (``india-lgd-index-api.vercel.app``, a mirror of lgdirectory.gov.in);
* **districts** - LGD districts per state from the same mirror;
* **cities** - the LGD *Urban Local Bodies* dump and the *Statewise ULB
  Coverage* dump (village-level ULB -> district mapping) published as ``.7z``
  archives in https://github.com/ramSeraph/opendata/releases (GODL-India);
  each ULB gets its primary district (most villages; ties -> lowest code).

Group-ID codes are never renumbered: ``city_code`` values already present in
``data/geo/cities.csv`` are kept for their LGD codes, the pinned legacy values
(``MP09`` Indore, ``MH15`` Nashik) are forced, and only *new* ULBs receive the
next free code in their state. Files are written to a scratch directory,
validated there (prefix collisions abort before anything is published), and
only then copied into the output directory.

Run from the project root:

    # fetch everything, validate, and regenerate data/geo/*.csv in place
    .venv/bin/python -m scripts.fetch_lgd_data

    # or write the result elsewhere for inspection first
    .venv/bin/python -m scripts.fetch_lgd_data --out /tmp/geo_new

Review the result with ``git diff data/geo`` before committing, then run
``.venv/bin/python -m scripts.import_geo_data --apply`` to update the database
(idempotent; existing prefixes in the database win over the files).

Extracting the ``.7z`` archives needs ``py7zr`` (a maintenance-only
dependency, deliberately not in requirements.txt):

    .venv/bin/pip install py7zr
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import shutil
import tempfile
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from app.services.geo_service import DATA_DIR, validate_geo_files

STATES_URL = "https://india-lgd-index-api.vercel.app/v1/states"
DISTRICTS_URL = "https://india-lgd-index-api.vercel.app/v1/districts?state={code}"
ARCHIVE_BASE = (
    "https://github.com/ramSeraph/opendata/releases/download/lgd-archive-extra1"
)
ULB_ARCHIVE_URL = f"{ARCHIVE_BASE}/urban_local_bodies.Sep2026.7z"
COVERAGE_ARCHIVE_URL = f"{ARCHIVE_BASE}/statewise_ulbs_coverage.Sep2026.7z"

#: Legacy Group-ID codes that must never move (issued before this tool existed).
PINNED_CITY_CODES = {250901: "09", 251442: "15"}  # Indore, Nashik

#: Canonical LGD state name -> internal two-letter prefix. Extend this table
#: only when LGD adds a State/UT; existing mappings are never rewritten.
STATE_PREFIXES = {
    "Andaman And Nicobar Islands": "AN",
    "Andhra Pradesh": "AP",
    "Arunachal Pradesh": "AR",
    "Assam": "AS",
    "Bihar": "BR",
    "Chandigarh": "CH",
    "Chhattisgarh": "CG",
    "Delhi": "DL",
    "Goa": "GA",
    "Gujarat": "GJ",
    "Haryana": "HR",
    "Himachal Pradesh": "HP",
    "Jammu And Kashmir": "JK",
    "Jharkhand": "JH",
    "Karnataka": "KA",
    "Kerala": "KL",
    "Ladakh": "LA",
    "Lakshadweep": "LD",
    "Madhya Pradesh": "MP",
    "Maharashtra": "MH",
    "Manipur": "MN",
    "Meghalaya": "ML",
    "Mizoram": "MZ",
    "Nagaland": "NL",
    "Odisha": "OD",
    "Puducherry": "PY",
    "Punjab": "PB",
    "Rajasthan": "RJ",
    "Sikkim": "SK",
    "Tamil Nadu": "TN",
    "Telangana": "TG",
    "The Dadra And Nagar Haveli And Daman And Diu": "DD",
    "Tripura": "TR",
    "Uttarakhand": "UK",
    "Uttar Pradesh": "UP",
    "West Bengal": "WB",
}

STATE_HEADER = ["lgd_code", "name", "code", "is_union_territory"]
DISTRICT_HEADER = ["lgd_code", "name", "state_lgd_code"]
CITY_HEADER = [
    "lgd_code",
    "name",
    "state_lgd_code",
    "district_lgd_code",
    "district_name",
    "ulb_type_code",
    "census_2011_code",
    "city_code",
]

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

_UA = {"User-Agent": "jssgapi-geo-refresh/1.0"}


def _http(url: str) -> bytes:
    request = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
        return response.read()


def _fetch_states() -> list[dict]:
    payload = json.loads(_http(STATES_URL))
    if not payload.get("success") or not payload.get("data"):
        raise SystemExit(f"Unexpected states payload: {payload!r:.200}")
    return payload["data"]


def _fetch_districts(state_codes: list[str]) -> list[dict]:
    districts: list[dict] = []
    for code in state_codes:
        districts.extend(json.loads(_http(DISTRICTS_URL.format(code=code))))
    return districts


def _extract_newest_csv(url: str, prefix: str, dest: Path) -> Path:
    try:
        import py7zr
    except ImportError as exc:  # pragma: no cover - maintenance dependency
        raise SystemExit(
            "py7zr is required to extract the LGD archives: "
            ".venv/bin/pip install py7zr"
        ) from exc

    archive = dest / (prefix + ".7z")
    archive.write_bytes(_http(url))
    with py7zr.SevenZipFile(archive, mode="r") as bundle:
        bundle.extractall(path=dest)

    def _date_key(path: Path) -> int:
        match = re.search(r"\.(\d{2})([A-Z][a-z]{2})(\d{4})\.csv$", path.name)
        if not match:
            return 0
        day, month, year = match.groups()
        return int(f"{year}{_MONTHS[month]:02d}{day}")

    dated = [
        p for p in dest.iterdir()
        if p.is_file() and p.name.startswith(prefix) and _date_key(p)
    ]
    if not dated:
        raise SystemExit(f"No {prefix}*.csv found in {url}")
    return max(dated, key=_date_key)


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _primary_districts(coverage_rows: list[dict]) -> dict[str, tuple[str, str]]:
    """Local Body Code -> (primary district code, district name).

    The coverage dump is one row per *village* of a ULB; the primary district
    is whichever district covers most of its villages, ties broken by the
    lowest district code - deterministic, matching the committed data.
    """
    votes: dict[str, Counter[str]] = defaultdict(Counter)
    names: dict[str, dict[str, str]] = defaultdict(dict)
    for row in coverage_rows:
        body = row["Local Body Code"].strip()
        district = row["District Code"].strip()
        if not district:
            continue
        votes[body][district] += 1
        names[body][district] = row["District Name (In English)"].strip()
    primary: dict[str, tuple[str, str]] = {}
    for body, counts in votes.items():
        best = sorted(counts.items(), key=lambda kv: (-kv[1], int(kv[0])))[0][0]
        primary[body] = (best, names[body][best])
    return primary


def _build_rows(
    states: list[dict],
    districts: list[dict],
    ulb_rows: list[dict],
    coverage_rows: list[dict],
    existing_cities: dict[int, str],
    old_states: dict[int, dict],
) -> tuple[list[dict], list[dict], list[dict], int]:
    # states: keep the assigned 2-letter prefix for a known LGD code; a new
    # State/UT must be added to STATE_PREFIXES explicitly (never auto-derived).
    state_rows: list[dict] = []
    for state in sorted(states, key=lambda s: int(s["code"])):
        lgd = int(state["code"])
        prefix = old_states[lgd]["code"] if lgd in old_states else None
        if prefix is None:
            prefix = STATE_PREFIXES.get(state["name"])
        if prefix is None:
            raise SystemExit(
                f"New state/UT {state['name']!r} has no prefix; add it to "
                "STATE_PREFIXES in scripts/fetch_lgd_data.py and re-run."
            )
        state_rows.append(
            {
                "lgd_code": lgd,
                "name": state["name"],
                "code": prefix,
                "is_union_territory": "true" if state.get("type") == "U" else "false",
            }
        )

    district_rows = [
        {
            "lgd_code": int(d["code"]),
            "name": d["name"],
            "state_lgd_code": int(d["state_code"]),
        }
        for d in sorted(districts, key=lambda d: int(d["code"]))
    ]

    # cities: pinned codes win, then every known city keeps its code, then
    # new ULBs get the next free number within their state.
    primary = _primary_districts(coverage_rows)
    state_of = {int(r["Local Body Code"]): int(r["State Code"]) for r in ulb_rows}
    assigned: dict[int, str] = {}
    for lgd, code in PINNED_CITY_CODES.items():
        if lgd in state_of:
            assigned[lgd] = code
    for lgd, code in existing_cities.items():
        if lgd in state_of and lgd not in assigned:
            assigned[lgd] = code

    used: dict[int, set[int]] = defaultdict(set)
    for lgd, code in assigned.items():
        used[state_of[lgd]].add(int(code))

    counters: dict[int, int] = defaultdict(lambda: 1)
    new_cities = 0
    city_rows: list[dict] = []
    for row in sorted(ulb_rows, key=lambda r: int(r["Local Body Code"])):
        lgd = int(row["Local Body Code"])
        state_lgd = int(row["State Code"])
        code = assigned.get(lgd)
        if code is None:
            while counters[state_lgd] in used[state_lgd]:
                counters[state_lgd] += 1
            code = f"{counters[state_lgd]:02d}"
            used[state_lgd].add(counters[state_lgd])
            assigned[lgd] = code
            new_cities += 1
        district_code, district_name = primary.get(str(lgd), ("", ""))
        city_rows.append(
            {
                "lgd_code": lgd,
                "name": row["Local Body Name (In English)"].strip(),
                "state_lgd_code": state_lgd,
                "district_lgd_code": district_code,
                "district_name": district_name,
                "ulb_type_code": row["Localbody Type Code"].strip(),
                "census_2011_code": row["Census 2011 Code"].strip(),
                "city_code": code,
            }
        )

    return state_rows, district_rows, city_rows, new_cities


def _write(path: Path, rows: list[dict], header: list[str]) -> None:
    # CRLF matches the committed seed files (csv.writer's default dialect), so
    # a refresh produces a content-only diff.
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")


def run(out_dir: Path) -> int:
    states = _fetch_states()
    districts = _fetch_districts([s["code"] for s in states])

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        ulb_csv = _extract_newest_csv(ULB_ARCHIVE_URL, "urban_local_bodies", tmp_path)
        coverage_csv = _extract_newest_csv(
            COVERAGE_ARCHIVE_URL, "statewise_ulbs_coverage", tmp_path
        )
        ulb_rows = _read_csv(ulb_csv)
        coverage_rows = _read_csv(coverage_csv)

        old_states = {int(r["lgd_code"]): r for r in _read_csv(DATA_DIR / "states.csv")}
        existing_cities = {
            int(r["lgd_code"]): r["city_code"]
            for r in _read_csv(DATA_DIR / "cities.csv")
        }
        state_rows, district_rows, city_rows, new_cities = _build_rows(
            states, districts, ulb_rows, coverage_rows, existing_cities, old_states
        )

        # Stage + validate before publishing: a collision can never reach
        # data/geo, let alone the database.
        stage = tmp_path / "staged"
        stage.mkdir()
        _write(stage / "states.csv", state_rows, STATE_HEADER)
        _write(stage / "districts.csv", district_rows, DISTRICT_HEADER)
        _write(stage / "cities.csv", city_rows, CITY_HEADER)
        counts = validate_geo_files(stage)
        out_dir.mkdir(parents=True, exist_ok=True)
        for header_row in ("states.csv", "districts.csv", "cities.csv"):
            shutil.copyfile(stage / header_row, out_dir / header_row)

    print(
        json.dumps(
            {
                "out_dir": str(out_dir),
                **counts,
                "cities_newly_numbered": new_cities,
            },
            indent=2,
        )
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.fetch_lgd_data",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DATA_DIR,
        help="Directory to write states.csv/districts.csv/cities.csv into "
        "(default: data/geo). Staged and validated before anything is copied.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run(out_dir=parse_args(argv).out)


if __name__ == "__main__":
    raise SystemExit(main())
