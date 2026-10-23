"""RTO-aware helpers shared by the geo API and Group-ID resolution.

The checked-in ``app.core.rto_codes`` mapping is the source of truth for the
city code that goes into a Group ID. Most cities resolve through
``get_rto_code``; a few LGD rows need an explicit alias because their names do
not contain the RTO's city/zone name (notably Delhi's municipal corporations).
"""

from __future__ import annotations

from app.core.rto_codes import RTO_BY_STATE, get_rto_code

#: State/UT codes covered by the checked-in RTO mapping.
RTO_STATE_CODES: frozenset[str] = frozenset(RTO_BY_STATE)

#: LGD city code -> RTO code for rows whose names cannot be matched directly.
#: Delhi's LGD layer uses municipal corporations rather than the RTO zones.
DELHI_RTO_OVERRIDES: dict[int, str] = {
    276400: "07",  # East Delhi Municipal Corporation -> DL07
    276401: "06",  # North Delhi Municipal Corporation -> DL06
    276402: "03",  # South Delhi Municipal Corporation -> DL03
    276447: "02",  # New Delhi Municipal Council -> DL02
}


def rto_code_for_city(
    state_code: str,
    city_name: str,
    lgd_code: int | None = None,
) -> str | None:
    """Return the two-digit RTO code for a city, or ``None`` when unmapped.

    ``lgd_code`` is optional; when supplied it is used for the explicit alias
    overrides before falling back to the normal name-based lookup.
    """
    state_code = state_code.strip().upper()
    if state_code == "DL" and lgd_code is not None:
        override = DELHI_RTO_OVERRIDES.get(lgd_code)
        if override is not None:
            return override
    return get_rto_code(state_code, city_name)
