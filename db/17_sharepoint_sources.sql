-- SharePoint files the app reads, edited in Settings > SharePoint.
--
-- sharepoint_sources: one row per file. The first group of columns is set in
-- Settings; the second group is status, written by the app each time it checks
-- or downloads the file. source_key never changes once a row exists - the
-- Buying Requirements code finds its two files by key ('daily_buyers',
-- 'cars_bought').
--
-- sharepoint_refresh_jobs: one row per download, run by the worker route
-- /api/sharepoint/refresh-run. The partial unique index allows only one queued
-- or running job per file, so two requests at once share one download.
--
-- The app creates both tables itself on first use
-- (utils/sharepoint_sources._ensure_tables), and adds the four existing files,
-- so running this by hand is only needed to set them up ahead of time.
-- Safe to re-run.

CREATE TABLE IF NOT EXISTS sharepoint_sources (
    source_key         VARCHAR(50)  PRIMARY KEY,
    name               VARCHAR(100) NOT NULL,
    file_type          VARCHAR(30)  NOT NULL,   -- kpi_daily_metrics, daily_buyers_sheet, cars_bought
    url                TEXT         NOT NULL,   -- any SharePoint link to the file
    read_kind          VARCHAR(10)  NOT NULL DEFAULT 'sheet',  -- 'sheet' (cell range) or 'table' (named Excel table)
    sheet_or_table     VARCHAR(100) NOT NULL,
    max_col            VARCHAR(3),              -- sheet only: last column read
    max_row            INTEGER,                 -- sheet only: last row read
    date_header        VARCHAR(100),            -- KPI Daily Metrics only
    column_names       JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- KPI: header -> metric name; others: field -> [headers]
    check_minutes      INTEGER      NOT NULL DEFAULT 15,
    enabled            BOOLEAN      NOT NULL DEFAULT TRUE,
    sort_order         INTEGER      NOT NULL DEFAULT 0,
    updated_by         VARCHAR(100),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_checked_at    TIMESTAMPTZ,             -- last time SharePoint was asked when the file was saved
    last_saved_at      TIMESTAMPTZ,             -- the answer: when the file was last saved
    copy_saved_at      TIMESTAMPTZ,             -- when the file was saved, for the copy we hold
    last_downloaded_at TIMESTAMPTZ,
    last_read_seconds  NUMERIC(7,2),
    last_row_count     INTEGER,
    last_error         TEXT,
    last_error_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sharepoint_refresh_jobs (
    job_id        VARCHAR(32)  PRIMARY KEY,
    source_key    VARCHAR(50)  NOT NULL,
    status        VARCHAR(10)  NOT NULL,       -- queued, running, done, failed, skipped
    reason        VARCHAR(20),                 -- changed, missing, reread
    file_saved_at TIMESTAMPTZ,                 -- SharePoint's saved time when the job was created
    requested_by  VARCHAR(100),
    requested_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    message       TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS sharepoint_refresh_jobs_one_open
    ON sharepoint_refresh_jobs (source_key) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS sharepoint_refresh_jobs_recent
    ON sharepoint_refresh_jobs (source_key, requested_at DESC);
