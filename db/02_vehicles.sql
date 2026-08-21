CREATE TABLE IF NOT EXISTS vehicles (
    vrm                         VARCHAR(10)     PRIMARY KEY,
    at_description              TEXT,
    at_factory_fitted_features  TEXT,
    at_first_registration_date  DATE,
    at_derivative_id            VARCHAR(64),
    cap_capid                   INTEGER,
    cap_capcode                 VARCHAR(50),
    cap_manufacturer            VARCHAR(100),
    cap_model                   VARCHAR(100),
    cap_derivative              TEXT,
    cap_registration_date       VARCHAR(20),
    created_at                  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vehicle_valuations (
    id                      BIGSERIAL       PRIMARY KEY,
    vrm                     VARCHAR(10)     NOT NULL REFERENCES vehicles(vrm),
    mileage                 INTEGER         NOT NULL, -- rounded valuation cache key
    reported_mileage        INTEGER,                  -- exact supplied odometer reading
    fetched_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    at_retail_valuation     INTEGER,
    at_rating               NUMERIC(6, 2),
    at_days_to_sell         NUMERIC(6, 2),
    at_market_condition     NUMERIC(6, 2),
    cap_live_clean          INTEGER,
    cap_live_retail         INTEGER,
    cap_monthly_clean       INTEGER,
    cap_monthly_retail      INTEGER,
    cap_rounded_mileage     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_vehicle_valuations_vrm_mileage_fetched
    ON vehicle_valuations (vrm, mileage, fetched_at DESC);

-- One row per VRM + supplier per import run. Append-only price history.
CREATE TABLE IF NOT EXISTS supplier_vehicle_listings (
    id                      BIGSERIAL       PRIMARY KEY,
    vrm                     VARCHAR(10)     NOT NULL REFERENCES vehicles(vrm),
    supplier_id             INTEGER         NOT NULL REFERENCES suppliers(supplier_id),
    imported_at             TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    mileage                 INTEGER,
    asking_price            NUMERIC(10, 2)
);

CREATE INDEX IF NOT EXISTS idx_svl_vrm_supplier
    ON supplier_vehicle_listings (vrm, supplier_id, imported_at DESC);

-- One row per VRM + supplier. Upserted every run. Tracks current state and removals.
CREATE TABLE IF NOT EXISTS supplier_vehicle_status (
    vrm                     VARCHAR(10)     NOT NULL REFERENCES vehicles(vrm),
    supplier_id             INTEGER         NOT NULL REFERENCES suppliers(supplier_id),
    first_seen_at           TIMESTAMPTZ     NOT NULL,
    last_seen_at            TIMESTAMPTZ     NOT NULL,
    current_price           NUMERIC(10, 2),
    previous_price          NUMERIC(10, 2),
    price_changed_at        TIMESTAMPTZ,
    removed_at              TIMESTAMPTZ,
    PRIMARY KEY (vrm, supplier_id)
);

CREATE INDEX IF NOT EXISTS idx_svs_active
    ON supplier_vehicle_status (supplier_id)
    WHERE removed_at IS NULL;
