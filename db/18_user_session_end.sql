-- How each session ended, and one clock for all session times.
--
-- started_by: 'password' (typed a password), 'cookie' (login cookie seen with
--             no row: signed in before this tracking began, or the sign-in
--             was not recorded), 'returned' (same cookie after a timeout)
-- end_reason: 'logout', 'timeout', 'forced', 'password_changed', 'user_deleted';
--             NULL while the session is open. An ended row is never changed.
--
-- The time columns become TIMESTAMPTZ. login_at and logout_at were written in
-- UK time; last_seen_at was UK time where only set at login/logout, and the
-- database's own time where activity moved it on.
--
-- The app does all of this itself on first use (db_utils._ensure_user_sessions),
-- so running this by hand is only needed to do it ahead of time.
--
-- Safe to re-run.

DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'user_sessions'
          AND column_name = 'login_at') = 'timestamp without time zone' THEN
        ALTER TABLE user_sessions
            ALTER COLUMN last_seen_at TYPE TIMESTAMPTZ USING
                CASE WHEN last_seen_at = login_at OR last_seen_at = logout_at
                     THEN last_seen_at AT TIME ZONE 'Europe/London'
                     ELSE last_seen_at AT TIME ZONE current_setting('TimeZone') END,
            ALTER COLUMN login_at  TYPE TIMESTAMPTZ USING login_at  AT TIME ZONE 'Europe/London',
            ALTER COLUMN logout_at TYPE TIMESTAMPTZ USING logout_at AT TIME ZONE 'Europe/London';
        UPDATE user_sessions SET last_seen_at = login_at WHERE last_seen_at < login_at;
    END IF;
END $$;

ALTER TABLE user_sessions
    ADD COLUMN IF NOT EXISTS started_by VARCHAR(20) NOT NULL DEFAULT 'password',
    ADD COLUMN IF NOT EXISTS ended_at   TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS end_reason VARCHAR(20) NULL;

CREATE INDEX IF NOT EXISTS idx_user_sessions_last_seen ON user_sessions (last_seen_at);

UPDATE user_sessions SET ended_at = logout_at, end_reason = 'logout'
WHERE logout_at IS NOT NULL AND ended_at IS NULL;
