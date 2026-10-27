-- Weekly Business Review Comments and the header bell's alerts.
-- routers/wbr_comments.py creates these itself the first time they are used
-- (PROD cluster, like kpi_layout); this file is the same change for running by hand.

CREATE TABLE IF NOT EXISTS wbr_comments (
    id          BIGSERIAL    PRIMARY KEY,
    target      VARCHAR(100) NOT NULL,          -- 'chart:<KPI row id>|<m|w|d>'
    parent_id   BIGINT       REFERENCES wbr_comments(id),   -- NULL = a thread, else a reply
    author      VARCHAR(100) NOT NULL,
    body        TEXT         NOT NULL,          -- plain text; @username marks a mention
    mentions    TEXT[]       NOT NULL DEFAULT '{}',
    assigned_to VARCHAR(100),
    resolved_at TIMESTAMPTZ,
    resolved_by VARCHAR(100),
    deleted_at  TIMESTAMPTZ,                    -- kept, marked deleted
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    edited_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS wbr_comments_target ON wbr_comments (target) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS user_alerts (
    id         BIGSERIAL    PRIMARY KEY,
    username   VARCHAR(100) NOT NULL,
    kind       VARCHAR(30)  NOT NULL,           -- 'mention' | 'reply'
    comment_id BIGINT       REFERENCES wbr_comments(id),
    from_user  VARCHAR(100),
    text       VARCHAR(300) NOT NULL,
    link       VARCHAR(500) NOT NULL,           -- relative to BASE_URL
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    read_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS user_alerts_user ON user_alerts (username, created_at DESC);
