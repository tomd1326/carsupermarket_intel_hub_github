-- Chart targets for the Google Ads Campaigns page.
--
-- One row per chart_key per date. Two metrics are stored per campaign, sessions
-- and events; everything else the page shows is derived from them:
--
--   conversion % target = summed events / summed sessions
--   aggregated target   = summed across the campaigns currently on screen
--   All events target   = component events summed, sessions counted once
--
-- Storing the rate separately would let 50 events, 1000 sessions and 10% coexist
-- while disagreeing with each other. Deriving it also means the target rolls up
-- to week and month by exactly the calculation the actual uses (a ratio of
-- summed events to summed sessions, never an average of daily percentages).
--
-- One row per date rather than a range: the CSV import planned for later has a
-- value per date, so it writes to this same table with no schema change and no
-- second code path on the read side.
--
-- Safe to re-run: creation is guarded, and there are no seed rows.

CREATE TABLE IF NOT EXISTS chart_targets (
    chart_key    VARCHAR(200)  NOT NULL,
    target_date  DATE          NOT NULL,
    target_value NUMERIC(14,4) NOT NULL,
    created_by   VARCHAR(100),
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chart_key, target_date)
);

-- No secondary index: the primary key already covers lookups by chart_key and
-- by (chart_key, target_date) range.
--
-- chart_key formats:
--   ga4:sessions:<campaign_id>            -- shared by every event
--   ga4:events:<event_name>:<campaign_id> -- one per event
--
-- Keyed by GA4 campaign ID, not name, so a renamed campaign keeps its targets.
-- Keys were name-based until db/15_ga4_targets_by_campaign_id.sql moved them.
--
-- There is deliberately no Aggregated key, no rate key and no All events key.
