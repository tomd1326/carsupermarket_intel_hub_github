-- Set-password links: account activation (a new user with no password yet) and
-- password reset, both through one page.
--
-- users.activated: FALSE until the user sets a password with their link.
-- Every existing user stays TRUE.
-- password_links: only a hash of the token is stored. A link is valid while
-- used_at and cancelled_at are NULL and expires_at is in the future.
--
-- The app does all of this itself on first use (db_utils._ensure_password_links),
-- so running this by hand is only needed to do it ahead of time.
--
-- Safe to re-run.

ALTER TABLE users ADD COLUMN IF NOT EXISTS activated BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS password_links (
    token_hash   CHAR(64) PRIMARY KEY,
    username     VARCHAR(100) NOT NULL REFERENCES users (username) ON DELETE CASCADE,
    purpose      VARCHAR(10) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ NOT NULL,
    used_at      TIMESTAMPTZ NULL,
    cancelled_at TIMESTAMPTZ NULL,
    created_by   VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS idx_password_links_username ON password_links (username);
