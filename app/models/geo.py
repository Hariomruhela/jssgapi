"""India administrative master data: State/UT → District → City.

Hierarchy (one level each, matching the Government of India Local Government
Directory):

* **State/UT** (:class:`GeoState`) - all 28 States and 8 Union Territories,
  keyed by their official LGD code. ``code`` is this project's own two-letter
  Group-ID prefix (``MP``, ``MH``, ...), assigned once and never derived from
  a city name.
* **District** (:class:`GeoDistrict`) - LGD districts, each belonging to
  exactly one State/UT. Present so the hierarchy is explicit; group creation
  does not require a district.
* **City** (:class:`GeoCity`) - LGD Urban Local Bodies (the LGD's city/town
  layer), each belonging to exactly one State/UT and optionally linked to its
  primary district. ``city_code``/``prefix`` are this project's deterministic
  Group-ID codes, not government-issued codes.

A city is never a state: ``Chandigarh`` exists once as a UT and once as a
city *inside* the Chandigarh UT, linked by the foreign key.

Identity is the LGD code everywhere. City names are not unique within a
state in the LGD (two Itanagar ULBs, etc.), so name lookups must handle
ambiguity explicitly.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class GeoState(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One of the 28 Indian States or 8 Union Territories."""

    __tablename__ = "geo_states"
    __table_args__ = (
        UniqueConstraint("lgd_code", name="uq_geo_states_lgd_code"),
        UniqueConstraint("name", name="uq_geo_states_name"),
        UniqueConstraint("code", name="uq_geo_states_code"),
    )

    #: Official LGD state/UT code, e.g. ``23`` for Madhya Pradesh.
    lgd_code: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Canonical LGD English name, e.g. ``Madhya Pradesh``.
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Project-owned two-letter Group-ID prefix, e.g. ``MP``. Not an LGD code.
    code: Mapped[str] = mapped_column(String(2), nullable=False)
    is_union_territory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    districts: Mapped[list[GeoDistrict]] = relationship(back_populates="state")
    cities: Mapped[list[GeoCity]] = relationship(back_populates="state")

    def __repr__(self) -> str:
        return f"<GeoState {self.code} {self.name}>"


class GeoDistrict(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An LGD district belonging to one State/UT."""

    __tablename__ = "geo_districts"
    __table_args__ = (
        UniqueConstraint("lgd_code", name="uq_geo_districts_lgd_code"),
        UniqueConstraint(
            "state_id", "name", name="uq_geo_districts_state_id_name"
        ),
        Index("ix_geo_districts_state_id", "state_id"),
    )

    #: Official LGD district code.
    lgd_code: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Canonical LGD English name.
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    state_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("geo_states.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    state: Mapped[GeoState] = relationship(back_populates="districts")

    def __repr__(self) -> str:
        return f"<GeoDistrict {self.lgd_code} {self.name}>"


class GeoCity(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An LGD Urban Local Body (city/town) belonging to one State/UT."""

    __tablename__ = "geo_cities"
    __table_args__ = (
        UniqueConstraint("lgd_code", name="uq_geo_cities_lgd_code"),
        UniqueConstraint("prefix", name="uq_geo_cities_prefix"),
        UniqueConstraint(
            "state_id", "city_code", name="uq_geo_cities_state_id_city_code"
        ),
        Index("ix_geo_cities_state_id", "state_id"),
        Index("ix_geo_cities_state_id_name_norm", "state_id", "name_norm"),
    )

    #: Official LGD Urban Local Body code - the city's identity.
    lgd_code: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Canonical LGD English name, e.g. ``Indore``.
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    #: Normalized (case-folded, whitespace-collapsed) name for lookups.
    name_norm: Mapped[str] = mapped_column(String(150), nullable=False)
    state_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("geo_states.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Primary district; NULL when the LGD coverage data has no mapping.
    district_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("geo_districts.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: LGD local-body type code (4 = Municipal Corporation, 5 = Municipality,
    #: 7 = Town Panchayat, ...). Stored as-is from the LGD dump.
    ulb_type_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Census 2011 town code; ``0``/NULL when the LGD has none.
    census_2011_code: Mapped[str | None] = mapped_column(String(12), nullable=True)
    #: Project-owned city code for the Group ID, e.g. ``09``. Deterministic,
    #: explicit, never derived from the city name, never reused.
    city_code: Mapped[str] = mapped_column(String(8), nullable=False)
    #: ``state.code + city_code``, e.g. ``MP09`` - the Group-ID sequence scope.
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    state: Mapped[GeoState] = relationship(back_populates="cities")
    district: Mapped[GeoDistrict | None] = relationship()

    def __repr__(self) -> str:
        return f"<GeoCity {self.prefix} {self.name}>"
