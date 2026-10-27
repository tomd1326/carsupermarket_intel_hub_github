-- Settings > Budgets: the monthly budget lines behind the Weekly Business Review.
-- routers/budgets.py creates these itself the first time they are used
-- (PROD cluster, like kpi_layout); this file is the same change for running by hand.

CREATE TABLE IF NOT EXISTS budget_lines (
    id         SERIAL       PRIMARY KEY,
    section    VARCHAR(50)  NOT NULL,           -- 'P&L', 'Volume Target', 'Cash Target'
    label      VARCHAR(200) NOT NULL,           -- as on the sheet, e.g. 'GPU-M (£/unit)'
    note       VARCHAR(100) NOT NULL DEFAULT '',-- the sheet's second column, e.g. '£/u'
    is_percent BOOLEAN      NOT NULL DEFAULT FALSE,  -- values stored as shown: 60% -> 60
    row_order  INTEGER      NOT NULL DEFAULT 0,
    map_source VARCHAR(100),                    -- 'snowflake' (daily_summary) or a ds_... data source id
    map_column VARCHAR(200),
    UNIQUE (section, label)
);

CREATE TABLE IF NOT EXISTS budget_values (
    line_id    INTEGER      NOT NULL REFERENCES budget_lines(id) ON DELETE CASCADE,
    month      CHAR(7)      NOT NULL,           -- 'YYYY-MM'
    value      NUMERIC,
    updated_by VARCHAR(100) NOT NULL,
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (line_id, month)
);
