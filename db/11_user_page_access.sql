-- Which pages each user may see. One row per granted page.
--
-- Created on demand by _ensure_user_page_access_table() in utils/db_utils.py,
-- the same as the settings and user_preferences tables — this file is the
-- canonical schema, not a migration that anything runs.
--
-- page_key values come from routers/page_registry.py.
--
-- The special key '__configured__' is not a page. It records that an
-- administrator has saved this user's access at least once. Without it, a user
-- whose every box was unticked would have no rows at all, which is
-- indistinguishable from a user who has never been configured — and an
-- unconfigured user is granted the default pages. It is written in the same
-- transaction as the grants.
CREATE TABLE IF NOT EXISTS user_page_access (
    username   VARCHAR(100) NOT NULL,
    page_key   VARCHAR(64)  NOT NULL,
    granted_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    granted_by VARCHAR(100),
    PRIMARY KEY (username, page_key)
);
