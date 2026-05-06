-- One row per login. Closed on explicit logout; last_seen_at updated on activity.
-- Session "length" is computed at query time: LEAST(logout_at, last_seen_at + 30min) - login_at
CREATE TABLE IF NOT EXISTS user_sessions (
    session_id   VARCHAR(64)  PRIMARY KEY,
    username     VARCHAR(100) NOT NULL,
    environment  VARCHAR(10)  NOT NULL,  -- 'prod', 'dev', 'local'
    login_at     TIMESTAMP    NOT NULL,
    last_seen_at TIMESTAMP    NOT NULL,
    logout_at    TIMESTAMP    NULL       -- NULL = session not explicitly closed
);

CREATE INDEX IF NOT EXISTS idx_user_sessions_username    ON user_sessions (username);
CREATE INDEX IF NOT EXISTS idx_user_sessions_environment ON user_sessions (environment);
CREATE INDEX IF NOT EXISTS idx_user_sessions_login_at    ON user_sessions (login_at DESC);

-- One row per meaningful user action (page view, tool invocation, file download, etc.)
-- metadata JSONB carries event-specific context without schema changes.
CREATE TABLE IF NOT EXISTS user_events (
    id           BIGSERIAL    PRIMARY KEY,
    session_id   VARCHAR(64)  NOT NULL REFERENCES user_sessions(session_id),
    username     VARCHAR(100) NOT NULL,
    environment  VARCHAR(10)  NOT NULL,
    event_type   VARCHAR(50)  NOT NULL,  -- e.g. 'page_view', 'cap_lookup', 'mot_check', 'upload_task'
    page         VARCHAR(100) NOT NULL,  -- e.g. '/buying-requirements', '/cap-lookup'
    metadata     JSONB        NULL,      -- arbitrary per-event context
    occurred_at  TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_events_session_id  ON user_events (session_id);
CREATE INDEX IF NOT EXISTS idx_user_events_username    ON user_events (username);
CREATE INDEX IF NOT EXISTS idx_user_events_event_type  ON user_events (event_type);
CREATE INDEX IF NOT EXISTS idx_user_events_occurred_at ON user_events (occurred_at DESC);
