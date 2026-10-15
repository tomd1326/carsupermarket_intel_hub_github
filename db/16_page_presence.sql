-- Live users circles beside the Home button. One row per open browser tab
-- (a new id on every page load), updated by each tab's heartbeat, marked
-- left_tab when the tab is closed, hidden or logged out. Rows older than an hour are
-- cleared by the heartbeat itself.
--
-- The app also creates this itself on first use
-- (db_utils._ensure_page_presence_table), so running this by hand is only
-- needed to set it up ahead of time.
--
-- Safe to re-run.

CREATE TABLE IF NOT EXISTS page_presence (
    tab_id        VARCHAR(64)  PRIMARY KEY,
    session_id    VARCHAR(64)  NOT NULL,
    username      VARCHAR(100) NOT NULL,
    environment   VARCHAR(10)  NOT NULL,  -- 'prod', 'dev', 'local'
    page          VARCHAR(200) NOT NULL,  -- path after BASE_URL, e.g. '/kpi'
    last_seen_at  TIMESTAMP    NOT NULL DEFAULT NOW(),
    seq           INTEGER      NOT NULL DEFAULT 0,      -- per-tab message number; lower ones are ignored
    left_tab      BOOLEAN      NOT NULL DEFAULT FALSE   -- tab closed/hidden; kept so its seq still counts
);

-- For a table created before seq/left_tab existed.
ALTER TABLE page_presence
    ADD COLUMN IF NOT EXISTS seq      INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS left_tab BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_page_presence_page      ON page_presence (environment, page);
CREATE INDEX IF NOT EXISTS idx_page_presence_last_seen ON page_presence (last_seen_at);
