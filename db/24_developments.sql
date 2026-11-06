-- Settings > Developments, What's New and Request A Feature (utils/developments.py).
-- In prod's database only, reached from every environment. The app creates and
-- updates these itself in _ensure_tables; this file is the same schema for reference.

CREATE TABLE IF NOT EXISTS developments (
    id SERIAL PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    dev_type VARCHAR(60) NOT NULL,                  -- 'Request' for a request until changed
    page_key VARCHAR(64),                           -- NULL = General / no page
    summary TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '',
    replaces TEXT NOT NULL DEFAULT '',
    guide TEXT NOT NULL DEFAULT '',
    faqs JSONB NOT NULL DEFAULT '[]',
    released_on DATE,
    -- new, needs_clarification, accepted, planned, in_progress, testing,
    -- ready_to_release, live, revisit_later, rejected. Only live shows on What's New.
    status VARCHAR(30) NOT NULL DEFAULT 'planned',
    updated_by VARCHAR(100),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source VARCHAR(10) NOT NULL DEFAULT 'own',      -- 'own' or 'request'
    requested_by VARCHAR(100),
    request_text TEXT NOT NULL DEFAULT '',          -- the requester's own words, never edited
    created_by VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmation_sent_at TIMESTAMPTZ,               -- NULL = the requester's email has not gone
    alert_sent_at TIMESTAMPTZ,                      -- NULL = the admin's alert has not gone
    email_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS development_emails (
    id SERIAL PRIMARY KEY,
    subject VARCHAR(200) NOT NULL,
    intro TEXT NOT NULL DEFAULT '',
    development_ids INTEGER[] NOT NULL,
    development_titles JSONB NOT NULL DEFAULT '[]',
    html TEXT NOT NULL,
    plain_text TEXT NOT NULL,
    created_by VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS development_email_template (
    key VARCHAR(60) PRIMARY KEY,
    value TEXT NOT NULL,
    updated_by VARCHAR(100),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
