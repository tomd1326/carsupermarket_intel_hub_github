-- 0% finance subsidy rate schedules.
--
-- Lenders quote the subsidy we pay as a % of balance at a handful of benchmark
-- terms only (typically 12/24/36/48/60). A customer agreement can run to any
-- whole month in between, so the page at /subsidy-rates fills the gaps by
-- straight-line interpolation between adjacent benchmarks.
--
-- A schedule stays in force until the next schedule for the same company and
-- finance type begins, so rate changes are recorded by adding a new row rather
-- than editing the old one — historic agreements stay reproducible.

-- Safe to re-run: creation is guarded and the seed rows are ON CONFLICT DO NOTHING,
-- so applying this to an environment that already has it is a no-op.

CREATE TABLE IF NOT EXISTS subsidy_rate_schedules (
    id              SERIAL       PRIMARY KEY,
    finance_company VARCHAR(100) NOT NULL,
    finance_type    VARCHAR(10)  NOT NULL,
    effective_from  DATE         NOT NULL,
    created_by      VARCHAR(100),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT subsidy_rate_schedules_type_chk CHECK (finance_type IN ('HP', 'PCP')),
    CONSTRAINT subsidy_rate_schedules_uniq UNIQUE (finance_company, finance_type, effective_from)
);

-- The benchmark terms as quoted by the lender. Everything between them is
-- derived at read time and deliberately not stored.
CREATE TABLE IF NOT EXISTS subsidy_rate_benchmarks (
    schedule_id INTEGER      NOT NULL REFERENCES subsidy_rate_schedules(id) ON DELETE CASCADE,
    term_months SMALLINT     NOT NULL,
    rate_pct    NUMERIC(7,3) NOT NULL,
    PRIMARY KEY (schedule_id, term_months),
    CONSTRAINT subsidy_rate_benchmarks_term_chk CHECK (term_months BETWEEN 1 AND 120),
    CONSTRAINT subsidy_rate_benchmarks_rate_chk CHECK (rate_pct >= 0)
);

CREATE INDEX IF NOT EXISTS subsidy_rate_schedules_lookup
    ON subsidy_rate_schedules (finance_company, finance_type, effective_from DESC);


-- ── Rates effective 1 June 2026 ───────────────────────────────────────────────
-- We do not offer terms below 24 months, so the 12-month rates the lenders quote
-- are deliberately not loaded. Black Horse also quotes PCP to 49 months only.

INSERT INTO subsidy_rate_schedules (finance_company, finance_type, effective_from, created_by) VALUES
    ('Black Horse Deal Saver', 'HP',  DATE '2026-06-01', 'tdrayson'),
    ('Black Horse Deal Saver', 'PCP', DATE '2026-06-01', 'tdrayson'),
    ('Santander 0%',           'HP',  DATE '2026-06-01', 'tdrayson'),
    ('Santander 0%',           'PCP', DATE '2026-06-01', 'tdrayson')
ON CONFLICT ON CONSTRAINT subsidy_rate_schedules_uniq DO NOTHING;

INSERT INTO subsidy_rate_benchmarks (schedule_id, term_months, rate_pct)
SELECT s.id, v.term_months, v.rate_pct
FROM subsidy_rate_schedules s
JOIN (VALUES
    ('Black Horse Deal Saver', 'HP',  24, 7.264),
    ('Black Horse Deal Saver', 'HP',  36, 10.499),
    ('Black Horse Deal Saver', 'HP',  48, 13.583),
    ('Black Horse Deal Saver', 'HP',  49, 13.583),
    ('Black Horse Deal Saver', 'HP',  60, 16.523),
    ('Black Horse Deal Saver', 'PCP', 24, 9.424),
    ('Black Horse Deal Saver', 'PCP', 36, 13.716),
    ('Black Horse Deal Saver', 'PCP', 48, 17.919),
    ('Black Horse Deal Saver', 'PCP', 49, 17.919),
    ('Santander 0%',           'HP',  24, 7.793),
    ('Santander 0%',           'HP',  36, 11.207),
    ('Santander 0%',           'HP',  48, 14.485),
    ('Santander 0%',           'HP',  60, 16.754),
    ('Santander 0%',           'PCP', 24, 10.368),
    ('Santander 0%',           'PCP', 36, 13.813),
    ('Santander 0%',           'PCP', 48, 16.725)
) AS v (finance_company, finance_type, term_months, rate_pct)
  ON v.finance_company = s.finance_company
 AND v.finance_type    = s.finance_type
WHERE s.effective_from = DATE '2026-06-01'
ON CONFLICT (schedule_id, term_months) DO NOTHING;
