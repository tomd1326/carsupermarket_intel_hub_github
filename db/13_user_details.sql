-- Name and email for each login, typed in by hand on the User Accounts page.
-- All optional. The app also adds these columns itself on first use
-- (db_utils._ensure_user_detail_columns), so running this by hand is only
-- needed to set them up ahead of time.
--
-- Safe to re-run.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS first_name VARCHAR(100),
    ADD COLUMN IF NOT EXISTS last_name  VARCHAR(100),
    ADD COLUMN IF NOT EXISTS email      VARCHAR(255);
