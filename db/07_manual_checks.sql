-- Manual (Assess Vehicle) checks share the vehicles / vehicle_valuations tables
-- with the supplier evaluator — the same VRM checked either way is the same
-- data, just presented differently. These columns record what the evaluator
-- never needed:
--
--   vehicles.at_vin           VIN is returned by the AT /vehicles call at no
--                             extra cost, but the evaluator never captured it.
--   vehicle_valuations.source 'evaluator' (supplier list run) or 'manual'
--                             (Assess Vehicle lookup), so each check can be
--                             labelled in the shared history.
--   vehicle_valuations.checked_by  username for manual checks, NULL for
--                             evaluator runs (those are attributed to a task).

ALTER TABLE vehicles
    ADD COLUMN IF NOT EXISTS at_vin VARCHAR(20);

ALTER TABLE vehicle_valuations
    ADD COLUMN IF NOT EXISTS source     VARCHAR(20) NOT NULL DEFAULT 'evaluator',
    ADD COLUMN IF NOT EXISTS checked_by VARCHAR(100);

-- Drives the per-VRM check history shown in the Assess Vehicle popup.
CREATE INDEX IF NOT EXISTS idx_vehicle_valuations_vrm_fetched
    ON vehicle_valuations (vrm, fetched_at DESC);

-- Drives the "everything checked in the last 30 days" list. The two indexes
-- above both lead with vrm, so neither helps a date-range scan across all
-- VRMs — without this the recent list seq-scans the whole table, which grows
-- by one row per vehicle per supplier import.
CREATE INDEX IF NOT EXISTS idx_vehicle_valuations_fetched
    ON vehicle_valuations (fetched_at DESC);
