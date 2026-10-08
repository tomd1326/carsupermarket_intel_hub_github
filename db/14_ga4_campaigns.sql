-- Campaigns shown on the Google Ads Campaigns page.
--
-- One row per GA4 campaign ID, added and removed from the page's Settings
-- popup. The ID is stored rather than the name because campaigns get renamed;
-- the page shows each campaign's current name, read from Snowflake.
--
-- Removing a campaign deletes its row here only. Its targets in chart_targets
-- are kept, so they come back if the campaign is added again.
--
-- The app creates this table itself on first use
-- (db_utils._ensure_ga4_campaigns_table), so running this by hand is only
-- needed to set it up ahead of time. Safe to re-run.

CREATE TABLE IF NOT EXISTS ga4_campaigns (
    campaign_id VARCHAR(50)  NOT NULL PRIMARY KEY,
    added_by    VARCHAR(100),
    added_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
