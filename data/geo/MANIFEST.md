# India administrative master data (seed files)

## Files

| File | Rows | Contents |
|---|---|---|
| `states.csv` | 36 | All 28 States + 8 Union Territories |
| `districts.csv` | 784 | All districts, each linked to its State/UT |
| `cities.csv` | 5051 | All Urban Local Bodies (cities/towns) from the LGD |

## Source

* **States / districts**: Government of India Local Government Directory (LGD),
  Ministry of Panchayati Raj — https://lgdirectory.gov.in/ , retrieved 21 Oct 2026
  through the community mirror `https://india-lgd-index-api.vercel.app`
  (source snapshot date `23Sep2026`, mirror of
  https://github.com/ramSeraph/opendata , GODL-India licence).
* **Cities/towns**: LGD *Urban Local Bodies* dump
  `urban_local_bodies.30Sep2026.csv` from
  https://github.com/ramSeraph/opendata/releases (lgd-archive-extra1),
  extracted from lgdirectory.gov.in. GODL-India licence.
* **ULB → district mapping**: LGD *Statewise ULB Coverage* dump
  `statewise_ulbs_coverage.30Sep2026.csv` from the same archive. Each ULB is
  assigned its **primary district** (the district covering the most of its
  villages; ties broken by lowest district LGD code). Some ULBs genuinely span
  more than one district — only the primary is stored.

## Column meanings

### `states.csv`
`lgd_code` — official LGD state/UT code. `name` — canonical LGD English name.
`code` — this project's own two-letter Group-ID prefix (`MP`, `MH`, ...). These
follow the common Indian vehicle-registration state-code convention but are
**internal project codes, not LGD codes**; they are assigned once and never
change. `is_union_territory` — `true` for the 8 UTs.

### `districts.csv`
`lgd_code` — official LGD district code. `name` — canonical LGD English name.
`state_lgd_code` — parent State/UT.

### `cities.csv`
`lgd_code` — official LGD Urban Local Body code (the city/town's identity).
`name` — canonical LGD English name. `state_lgd_code` — parent State/UT.
`district_lgd_code` / `district_name` — primary district (may be empty if the
coverage dump has no mapping). `ulb_type_code` — LGD local-body type code
(4 = Municipal Corporation, 5 = Municipality, 7 = Town Panchayat, ...).
`census_2011_code` — Census 2011 town code (`0` when none). `city_code` — this
project's explicit, deterministic Group-ID city code (2+ digits). Assigned by
sorting each state's ULBs by LGD code and numbering from `01`, **except** two
pinned values that preserve IDs generated before this dataset existed:
`MP09` = Indore, `MH15` = Nashik. These codes are internal project codes, not
government-issued.

## Guarantees

* Every city has a unique `state_lgd_code + city_code` prefix (verified at
  generation time; re-verified by the importer and enforced by a DB unique
  constraint).
* City names are **not** unique within a state in LGD (e.g. two Itanagar ULBs);
  `lgd_code` is the identity, and name-based group creation rejects ambiguous
  matches instead of guessing.
* Lakshadweep currently has no Urban Local Body in the LGD dump, so no city is
  configured there.

## Refreshing

`scripts/fetch_lgd_data.py` re-downloads the upstream dumps and regenerates
these CSVs, preserving already-assigned `city_code` values (never renumbers an
existing city). The importer (`scripts/import_geo_data.py`) is idempotent and
safe to re-run; it never duplicates rows and never changes the code/prefix of
a city that already exists in the database.
