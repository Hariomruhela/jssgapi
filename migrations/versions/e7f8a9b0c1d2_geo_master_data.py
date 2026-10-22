"""geo master data: states/UTs, districts, cities (LGD-sourced)

Revision ID: e7f8a9b0c1d2
Revises: d5e6f7a8b9c0
Create Date: 2026-10-21 00:00:00.000000

Creates the India administrative master-data tables backing Group ID
generation and location validation:

* ``geo_states``    - 28 States + 8 Union Territories (official LGD codes)
* ``geo_districts`` - LGD districts, each under one State/UT
* ``geo_cities``    - LGD Urban Local Bodies (cities/towns), each under one
  State/UT, with this project's deterministic Group-ID ``city_code``/``prefix``

Only new tables - no existing table, column, constraint or row is touched.
Idempotent (``IF NOT EXISTS`` / ``ON CONFLICT DO NOTHING``), so it is safe to
run against a partially-migrated database. Rows are loaded by
``scripts/import_geo_data.py``, not by this migration.
"""

from __future__ import annotations

from alembic import op

revision = "e7f8a9b0c1d2"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS geo_states (
            id UUID PRIMARY KEY,
            lgd_code INTEGER NOT NULL,
            name VARCHAR(120) NOT NULL,
            code VARCHAR(2) NOT NULL,
            is_union_territory BOOLEAN NOT NULL DEFAULT FALSE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by UUID,
            updated_by UUID,
            CONSTRAINT uq_geo_states_lgd_code UNIQUE (lgd_code),
            CONSTRAINT uq_geo_states_name UNIQUE (name),
            CONSTRAINT uq_geo_states_code UNIQUE (code)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS geo_districts (
            id UUID PRIMARY KEY,
            lgd_code INTEGER NOT NULL,
            name VARCHAR(120) NOT NULL,
            state_id UUID NOT NULL REFERENCES geo_states (id) ON DELETE CASCADE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by UUID,
            updated_by UUID,
            CONSTRAINT uq_geo_districts_lgd_code UNIQUE (lgd_code),
            CONSTRAINT uq_geo_districts_state_id_name UNIQUE (state_id, name)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_geo_districts_state_id ON geo_districts (state_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS geo_cities (
            id UUID PRIMARY KEY,
            lgd_code INTEGER NOT NULL,
            name VARCHAR(150) NOT NULL,
            name_norm VARCHAR(150) NOT NULL,
            state_id UUID NOT NULL REFERENCES geo_states (id) ON DELETE CASCADE,
            district_id UUID REFERENCES geo_districts (id) ON DELETE SET NULL,
            ulb_type_code INTEGER,
            census_2011_code VARCHAR(12),
            city_code VARCHAR(8) NOT NULL,
            prefix VARCHAR(16) NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by UUID,
            updated_by UUID,
            CONSTRAINT uq_geo_cities_lgd_code UNIQUE (lgd_code),
            CONSTRAINT uq_geo_cities_prefix UNIQUE (prefix),
            CONSTRAINT uq_geo_cities_state_id_city_code UNIQUE (state_id, city_code)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_geo_cities_state_id ON geo_cities (state_id)"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_geo_cities_state_id_name_norm
            ON geo_cities (state_id, name_norm)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS geo_cities")
    op.execute("DROP TABLE IF EXISTS geo_districts")
    op.execute("DROP TABLE IF EXISTS geo_states")
