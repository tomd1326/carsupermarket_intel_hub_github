-- AI Knowledge (Settings > AI > Knowledge): the entries every Ask AI reads
-- before answering - table descriptions, business rules, instructions.
--
-- id:      text, made by the app, so Copy Prod To Dev can copy rows as they are.
-- kind:    table / yaml / document / instruction / learned.
-- scope:   'all' (every page's Ask AI) or one page key, e.g. 'kpi'.
-- status:  'active' entries reach the model; 'proposed' ones (the starting
--          entries, and anything Ask AI suggests remembering) wait for Tom to
--          approve them in Settings.
-- enabled: an active entry can be switched off without deleting it.
--
-- The app does all of this itself on first use (utils/ai_knowledge._ensure_table),
-- so running this by hand is only needed to do it ahead of time.
--
-- Safe to re-run.

CREATE TABLE IF NOT EXISTS ai_knowledge (
    id         VARCHAR(40) PRIMARY KEY,
    title      VARCHAR(200) NOT NULL,
    kind       VARCHAR(20) NOT NULL,
    scope      VARCHAR(50) NOT NULL DEFAULT 'all',
    content    TEXT NOT NULL DEFAULT '',
    enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    status     VARCHAR(10) NOT NULL DEFAULT 'active',
    updated_by VARCHAR(100),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
