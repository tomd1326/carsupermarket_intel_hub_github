-- HPI Spec Check results (factory-fit options).
--
-- One row per VRM, kept forever. Every Spec Check is a billable HPI call, so
-- this table exists to make sure a vehicle is only ever paid for once: the page
-- reads from here and only calls HPI when there is no row at all.
--
-- Factory options are fixed for the life of the car — they were fitted when it
-- was built — so unlike a valuation there is no age at which this data goes out
-- of date and needs re-requesting.

CREATE TABLE IF NOT EXISTS vehicle_spec_checks (
    vrm                 VARCHAR(10)     PRIMARY KEY,
    -- 1 = data returned, 103 = no spec data for this vehicle,
    -- 120 = blocked make (AVX03 only). All three are stored: a 103 is a real
    -- answer worth remembering so the same vehicle is not paid for twice.
    status_code         SMALLINT        NOT NULL,
    status_message      TEXT,
    base_price          NUMERIC(10, 2),
    options_price       NUMERIC(10, 2),
    total_price         NUMERIC(10, 2),
    options_rating      NUMERIC(3, 1),
    -- The full option list as returned, Standard and Optional together.
    -- JSONB rather than a child table: it is always read whole, for one VRM,
    -- and never filtered or joined on in SQL. Roughly 20-30 KB per vehicle
    -- before TOAST compression.
    options             JSONB,
    option_count        SMALLINT,
    optional_count      SMALLINT,
    fetched_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    fetched_by          VARCHAR(100)
);

-- Finding which vehicles have usable spec data, for the future work on mapping
-- HPI options onto Autotrader feature selections.
CREATE INDEX IF NOT EXISTS idx_vehicle_spec_checks_available
    ON vehicle_spec_checks (status_code)
    WHERE status_code = 1;
