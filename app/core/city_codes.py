"""The explicit Group-ID code mapping: the codes every Group ID is built from.

A Group ID is ``STATE_CODE + CITY_CODE + SEQUENCE``:

* **state code** - the two-letter prefix of the State/UT, stored in
  ``geo_states.code`` (``MP``, ``MH``, ...) and defined once in
  ``data/geo/states.csv``;
* **city code** - this project's own code for a city, stored in
  ``geo_cities.city_code`` and defined in ``data/geo/cities.csv``;
* **sequence** - the per-city counter, ``001`` for the first group in that
  state/city pair (see ``app.services.group_id_service``).

The codes are **assigned, never derived**. A city code is not computed from a
row id, a location id, a name, an ASCII value, a checksum or a position in a
list - it is looked up in this mapping. The codes below are the ones this
project requires; every other city keeps the code it was assigned when the
LGD master data was imported. A code is unique inside its state, is assigned
once and never changes, so an ID that has already been issued keeps pointing
at the same city.

The codes required for Madhya Pradesh and Maharashtra:

===============  ==================  =========
City             LGD city code       City code
===============  ==================  =========
Bhopal           250946              ``04``
Indore           250901              ``09``
Ujjain           250851              ``13``
Nashik           251442              ``15``
===============  ==================  =========

so the first group in Bhopal is ``MP04001``, in Indore ``MP09001``, in Ujjain
``MP13001`` and in Nashik ``MH15001``.

The mapping is keyed by the official LGD Urban Local Body code
(``geo_cities.lgd_code``), not by the city name: the LGD has more than one
Itanagar ULB, so a name is not an identity.
"""

from __future__ import annotations

#: The city codes this project requires, keyed by the LGD Urban Local Body
#: code of the city. Assigned by hand, never computed. A city that is not
#: listed here keeps the code assigned by the geo master data.
REQUIRED_CITY_CODES: dict[int, str] = {
    250851: "13",  # Ujjain, Madhya Pradesh
    250901: "09",  # Indore, Madhya Pradesh
    250946: "04",  # Bhopal, Madhya Pradesh
    251442: "15",  # Nashik, Maharashtra
}

#: The codes of the cities that gave their code up to a required city. They
#: are pinned so that a refresh of the LGD master data can never hand the
#: code back to them and collide with the city that took it over.
REHOMED_CITY_CODES: dict[int, str] = {
    248068: "414",  # Churhat, Madhya Pradesh - MP04 belongs to Bhopal
    250673: "415",  # Sheopur, Madhya Pradesh - MP13 belongs to Ujjain
}
