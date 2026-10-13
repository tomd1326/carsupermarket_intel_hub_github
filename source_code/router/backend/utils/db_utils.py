import datetime
import json
import logging
import os
import psycopg2
import psycopg2.extras
import psycopg2.pool
from contextlib import contextmanager
from botocore.exceptions import UnauthorizedSSOTokenError
from fastapi import HTTPException
from utils.secrets_manager_client import get_secret

logger = logging.getLogger(__name__)

_db_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_vehicle_db_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_prod_pricer_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def get_db_credentials():
    if os.getenv('AWS_REGION') == 'eu-west-1':
        secret = 'prod/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials'
    else:
        # LOCAL_ENV=1 (local) and us-east-2 (dev Lambda) both use dev secret
        secret = 'dev/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials'
    data = get_secret(secret)
    return {
        'host': data['endpoint'],
        'user': data['username'],
        'password': data['password'],
        'database': 'carsupermarket_pricer',
        'port': int(data.get('port', 5432)),
    }


def get_vehicle_db_credentials():
    """Returns prod credentials on prod Lambda (eu-west-1), dev credentials on dev Lambda (us-east-2)."""
    if os.getenv('LOCAL_ENV') == '1':
        # In local dev, honour explicit overrides; otherwise fall through to the environment secret.
        host = os.getenv('VEHICLE_DB_HOST') or os.getenv('DB_HOST')
        if host:
            return {
                'host': host,
                'user': os.getenv('VEHICLE_DB_USER', os.getenv('DB_USER', 'test')),
                'password': os.getenv('VEHICLE_DB_PASSWORD', os.getenv('DB_PASSWORD', 'password')),
                'database': os.getenv('VEHICLE_DB_DATABASE', os.getenv('DB_DATABASE', 'test_db')),
                'port': int(os.getenv('VEHICLE_DB_PORT', os.getenv('DB_PORT', 5432))),
            }
    if os.getenv('AWS_REGION') == 'eu-west-1':
        varname = 'prod/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials'
    else:
        varname = 'dev/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials'
    data = get_secret(varname)
    return {
        'host': data['endpoint'],
        'user': data['username'],
        'password': data['password'],
        'database': 'carsupermarket_pricer',
        'port': int(data.get('port', 5432)),
    }


_KEEPALIVE_KWARGS = {
    'keepalives':            1,
    'keepalives_idle':       60,   # seconds before sending first keepalive probe
    'keepalives_interval':   10,   # seconds between probes
    'keepalives_count':      5,    # drop connection after this many failed probes
}


def _is_prod_lambda() -> bool:
    return os.getenv('AWS_REGION') == 'eu-west-1'


def _has_local_vehicle_override() -> bool:
    """True when local dev points the vehicle DB somewhere other than the environment secret."""
    return os.getenv('LOCAL_ENV') == '1' and bool(os.getenv('VEHICLE_DB_HOST') or os.getenv('DB_HOST'))


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _db_pool
    if _db_pool is None:
        creds = get_db_credentials()
        _db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=10,
            host=creds['host'], user=creds['user'], password=creds['password'],
            database=creds['database'], port=creds['port'],
            **_KEEPALIVE_KWARGS,
        )
    return _db_pool


def _get_prod_pricer_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Pool for reads that target the PROD pricer DB when it can be reached.

    On the prod Lambda that is the same cluster, user and database as the main
    pool, so the two share one pool and the container opens one set of
    connections instead of two.

    The deployed dev Lambda (us-east-2) has no network route to the prod
    cluster in eu-west-1, so there it also uses the main pool, which is the dev
    cluster. Deployed dev therefore has its own KPI layout and page names.

    Only local (over the VPN) keeps a separate pool that reads prod while the
    main pool points at dev. AWS sets AWS_LAMBDA_FUNCTION_NAME in every Lambda
    and never locally; LOCAL_ENV cannot be used, since `python main.py` runs
    locally without it.
    """
    if _is_prod_lambda() or os.getenv('AWS_LAMBDA_FUNCTION_NAME'):
        return _get_pool()

    global _prod_pricer_pool
    if _prod_pricer_pool is None:
        data = get_secret('prod/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials')
        _prod_pricer_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=5,
            host=data['endpoint'], user=data['username'], password=data['password'],
            database='carsupermarket_pricer', port=int(data.get('port', 5432)),
            **_KEEPALIVE_KWARGS,
        )
    return _prod_pricer_pool


def _get_vehicle_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Pool for vehicle reads.

    When deployed, get_vehicle_db_credentials() resolves to the same secret,
    endpoint, user and database as get_db_credentials() — prod secret on the
    prod Lambda, dev secret on the dev Lambda — so the two share one pool. Only
    a local VEHICLE_DB_HOST/DB_HOST override can point somewhere else, and that
    keeps its own pool.
    """
    if not _has_local_vehicle_override():
        return _get_pool()

    global _vehicle_db_pool
    if _vehicle_db_pool is None:
        creds = get_vehicle_db_credentials()
        _vehicle_db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=10,
            host=creds['host'], user=creds['user'], password=creds['password'],
            database=creds['database'], port=creds['port'],
            **_KEEPALIVE_KWARGS,
        )
    return _vehicle_db_pool


SETTINGS_DEFAULTS = {
    'number_to_buy':                    200,
    'max_days_sales':                   90,
    'max_dts':                          45,
    'min_gpu_m':                        0,
    'cap_workers':                      25,
    'cap_rate_limit':                   100,
    'at_workers':                       40,
    'at_rate_limit':                    60,
    'calc_buying_reqs':                 '0',
    'get_stock':                        '0',
    'enable_cars_bought':               '0',
    'kpi_ai_summary':                   '0',
    'mot_workers':                      10,
    'valuation_cache_hours':            36,
    # How long Buying Requirements reuses data before refetching. Minutes.
    # 0 on the Snowflake setting means "follow the dbt schedule only".
    'sharepoint_cache_minutes':         30,
    'snowflake_cache_minutes':          0,
    'cap_override_enabled':             '0',
    'cap_override_subscriber_id':       '',
    'cap_override_password':            '',
}

BOOL_SETTINGS = {'calc_buying_reqs', 'get_stock', 'enable_cars_bought', 'kpi_ai_summary', 'cap_override_enabled'}

# Per-user bool settings stored as e.g. calc_buying_reqs__buyer, get_stock__buyer.
PER_USER_BOOL_SETTINGS = {'calc_buying_reqs', 'get_stock'}

ADMIN_USER = 'admin'
ALWAYS_ENABLED_USERS = {'admin', 'tdrayson'}


def get_settings(conn, user: str = None) -> dict:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM settings")
            rows = cur.fetchall()
        row_map = dict(rows)
        result = dict(SETTINGS_DEFAULTS)
        STRING_SETTINGS = {'cap_override_subscriber_id', 'cap_override_password'}
        for key, value in rows:
            if key in BOOL_SETTINGS or key in STRING_SETTINGS or '__' in key:
                continue
            if key in result:
                result[key] = int(value)
        # Per-user bool settings: always-enabled users always True; others read from user-scoped key.
        for key in PER_USER_BOOL_SETTINGS:
            if user in ALWAYS_ENABLED_USERS:
                result[key] = True
            else:
                scoped_key = f'{key}__{user}' if user else key
                result[key] = row_map.get(scoped_key, '0') == '1'
        # Global bool settings (not per-user).
        result['enable_cars_bought'] = row_map.get('enable_cars_bought', '0') == '1'
        result['kpi_ai_summary'] = row_map.get('kpi_ai_summary', '0') == '1'
        result['cap_override_enabled'] = row_map.get('cap_override_enabled', '0') == '1'
        result['cap_override_subscriber_id'] = row_map.get('cap_override_subscriber_id', '')
        result['cap_override_password'] = row_map.get('cap_override_password', '')
        # valuation_cache_hours: integer, global.
        try:
            result['valuation_cache_hours'] = int(row_map.get('valuation_cache_hours', SETTINGS_DEFAULTS['valuation_cache_hours']))
        except (TypeError, ValueError):
            result['valuation_cache_hours'] = SETTINGS_DEFAULTS['valuation_cache_hours']
        return result
    except Exception:
        d = dict(SETTINGS_DEFAULTS)
        for key in PER_USER_BOOL_SETTINGS:
            d[key] = user in ALWAYS_ENABLED_USERS
        d['enable_cars_bought'] = False
        d['kpi_ai_summary'] = False
        d['cap_override_enabled'] = False
        d['cap_override_subscriber_id'] = ''
        d['cap_override_password'] = ''
        return d


def save_settings(conn, number_to_buy: int, max_days_sales: int, max_dts: int, min_gpu_m: int, cap_workers: int, cap_rate_limit: int, calc_buying_reqs: bool, get_stock: bool = False, enable_cars_bought: bool = False, kpi_ai_summary: bool = False, mot_workers: int = 10, at_workers: int = 40, at_rate_limit: int = 60, valuation_cache_hours: int = 36, cap_override_enabled: bool = False, cap_override_subscriber_id: str = '', cap_override_password: str = '', user: str = None):
    values = {
        'number_to_buy':                number_to_buy,
        'max_days_sales':               max_days_sales,
        'max_dts':                      max_dts,
        'min_gpu_m':                    min_gpu_m,
        'cap_workers':                  cap_workers,
        'cap_rate_limit':               cap_rate_limit,
        'at_workers':                   at_workers,
        'at_rate_limit':                at_rate_limit,
        'mot_workers':                  mot_workers,
        'valuation_cache_hours':        valuation_cache_hours,
        'cap_override_subscriber_id':   cap_override_subscriber_id,
        'cap_override_password':        cap_override_password,
    }
    # Per-user bool settings; always-enabled users' preferences are not stored (always True at read time).
    if user not in ALWAYS_ENABLED_USERS:
        suffix = f'__{user}' if user else ''
        values[f'calc_buying_reqs{suffix}'] = '1' if calc_buying_reqs else '0'
        values[f'get_stock{suffix}'] = '1' if get_stock else '0'
    # Global bool settings.
    values['enable_cars_bought'] = '1' if enable_cars_bought else '0'
    values['kpi_ai_summary'] = '1' if kpi_ai_summary else '0'
    values['cap_override_enabled'] = '1' if cap_override_enabled else '0'

    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS settings (key VARCHAR(100) PRIMARY KEY, value VARCHAR(250))"
        )
        for key, value in values.items():
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, str(value))
            )
    conn.commit()


def save_settings_partial(conn, values: dict):
    """
    Write only the given settings keys, leaving every other key untouched.

    save_settings() takes the whole settings set positionally, so calling it
    from a page that only renders one section would write defaults over the
    sections that page never showed. The per-section settings pages use this
    instead so each Save only touches its own fields.

    Bool values should already be normalised to '1'/'0' by the caller, and
    per-user keys should already carry their '__<user>' suffix.
    """
    if not values:
        return
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS settings (key VARCHAR(100) PRIMARY KEY, value VARCHAR(250))"
        )
        for key, value in values.items():
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, str(value))
            )
    conn.commit()


def _pool_getconn_or_raise(pool, label: str):
    try:
        return pool.getconn()
    except psycopg2.OperationalError as e:
        msg = next((l.strip() for l in str(e).splitlines() if l.strip()), str(e).strip())
        raise HTTPException(
            status_code=503,
            detail=f"Database connection failed — check VPN/network connectivity. Detail: {msg}",
        ) from e


@contextmanager
def get_database_connection():
    try:
        pool = _get_pool()
    except UnauthorizedSSOTokenError:
        logger.error("AWS SSO session has expired. Run: aws sso login")
        raise HTTPException(status_code=503, detail="AWS credentials expired — run: aws sso login --profile <your-profile>")
    except psycopg2.OperationalError as e:
        msg = next((l.strip() for l in str(e).splitlines() if l.strip()), str(e).strip())
        if 'password authentication failed' in msg or 'no pg_hba.conf entry' in msg:
            detail = f"Aurora credentials rejected — check Secrets Manager or VPN. Detail: {msg}"
        else:
            detail = f"Aurora connection failed — check VPN is connected. Detail: {msg}"
        raise HTTPException(status_code=503, detail=detail) from e

    connection = _pool_getconn_or_raise(pool, "pricer")
    try:
        yield connection
    finally:
        pool.putconn(connection)


# ── Per-user UI preferences ──────────────────────────────────────────────────
# Generic key/value store, JSONB values. Created on first save (same pattern as
# the settings table) so no migration script is needed in dev or prod.

def _ensure_user_preferences_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                username    VARCHAR(100) NOT NULL,
                pref_key    VARCHAR(100) NOT NULL,
                pref_value  JSONB        NOT NULL,
                updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                PRIMARY KEY (username, pref_key)
            )
            """
        )
    conn.commit()


def get_user_preference(conn, username: str, pref_key: str):
    """Returns the stored JSON value, or None if not set (or table doesn't exist yet)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pref_value FROM user_preferences WHERE username = %s AND pref_key = %s",
                (username, pref_key),
            )
            row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        conn.rollback()
        return None


def save_user_preference(conn, username: str, pref_key: str, pref_value) -> None:
    _ensure_user_preferences_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_preferences (username, pref_key, pref_value, updated_at) "
            "VALUES (%s, %s, %s, NOW()) "
            "ON CONFLICT (username, pref_key) DO UPDATE SET pref_value = EXCLUDED.pref_value, updated_at = NOW()",
            (username, pref_key, json.dumps(pref_value)),
        )
    conn.commit()


def delete_user_preference(conn, username: str, pref_key: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_preferences WHERE username = %s AND pref_key = %s",
                (username, pref_key),
            )
        conn.commit()
    except Exception:
        conn.rollback()


# ── User activity analytics ───────────────────────────────────────────────────

def record_login(conn, session_id: str, username: str, environment: str, login_at) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_sessions (session_id, username, environment, login_at, last_seen_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO NOTHING
            """,
            (session_id, username, environment, login_at, login_at),
        )
    conn.commit()


def record_logout(conn, session_id: str, last_seen_at) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_sessions SET logout_at = %s, last_seen_at = %s WHERE session_id = %s AND logout_at IS NULL",
            (last_seen_at, last_seen_at, session_id),
        )
    conn.commit()


def record_event(conn, session_id: str, username: str, environment: str,
                 event_type: str, page: str, metadata: dict | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_events (session_id, username, environment, event_type, page, metadata)
            SELECT %s, %s, %s, %s, %s, %s
            WHERE EXISTS (SELECT 1 FROM user_sessions WHERE session_id = %s)
            """,
            (session_id, username, environment, event_type, page,
             json.dumps(metadata) if metadata else None, session_id),
        )
        # Debounce last_seen_at: only write if >60s since last update to reduce churn
        cur.execute(
            """
            UPDATE user_sessions
            SET last_seen_at = NOW()
            WHERE session_id = %s AND last_seen_at < NOW() - INTERVAL '60 seconds'
            """,
            (session_id,),
        )
    conn.commit()


def get_last_login_per_user(conn) -> dict:
    """Returns {username: last_login_at_str} across all environments."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (username)
                username,
                login_at
            FROM user_sessions
            ORDER BY username, login_at DESC
            """
        )
        return {row[0]: row[1].strftime('%Y-%m-%d %H:%M') for row in cur.fetchall()}


def get_active_sessions_all_envs(conn, inactivity_minutes: int = 30) -> list:
    """Returns sessions active within the inactivity window, across all environments."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT username, environment, login_at, last_seen_at
            FROM user_sessions
            WHERE logout_at IS NULL
              AND last_seen_at >= NOW() - (INTERVAL '1 minute' * %s)
            ORDER BY last_seen_at DESC
            """,
            (inactivity_minutes,),
        )
        return [
            {
                'user': row[0],
                'environment': row[1],
                'login_time': row[2].strftime('%Y-%m-%d %H:%M'),
                'last_seen': row[3].strftime('%Y-%m-%d %H:%M'),
            }
            for row in cur.fetchall()
        ]


# ── User management (always prod) ────────────────────────────────────────────

def load_all_users(conn) -> dict:
    """Returns {username: pwd_hash} for all users."""
    with conn.cursor() as cur:
        cur.execute("SELECT username, pwd_hash FROM users")
        return {row[0]: row[1] for row in cur.fetchall()}


def upsert_user(conn, username: str, pwd_hash: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, pwd_hash) VALUES (%s, %s) "
            "ON CONFLICT (username) DO UPDATE SET pwd_hash = EXCLUDED.pwd_hash",
            (username, pwd_hash),
        )
    conn.commit()


def _ensure_user_detail_columns(conn) -> None:
    """Name and email columns, added on first use. db/13_user_details.sql is
    the same change for running by hand."""
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE users "
            "ADD COLUMN IF NOT EXISTS first_name VARCHAR(100), "
            "ADD COLUMN IF NOT EXISTS last_name  VARCHAR(100), "
            "ADD COLUMN IF NOT EXISTS email      VARCHAR(255)"
        )
    conn.commit()


def load_user_details(conn) -> dict:
    """Returns {username: {'first_name', 'last_name', 'email'}} for all users.
    Missing values come back as empty strings."""
    _ensure_user_detail_columns(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT username, first_name, last_name, email FROM users")
        return {
            row[0]: {'first_name': row[1] or '', 'last_name': row[2] or '', 'email': row[3] or ''}
            for row in cur.fetchall()
        }


def update_user_details(conn, details: dict) -> None:
    """Save name and email for several users in one transaction.
    details is {username: {'first_name', 'last_name', 'email'}}; empty strings
    are stored as NULL."""
    _ensure_user_detail_columns(conn)
    try:
        with conn.cursor() as cur:
            for username, d in details.items():
                cur.execute(
                    "UPDATE users SET first_name = %s, last_name = %s, email = %s WHERE username = %s",
                    (d['first_name'] or None, d['last_name'] or None, d['email'] or None, username),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def remove_user(conn, username: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE username = %s", (username,))
    conn.commit()


# ── Page access ──────────────────────────────────────────────────────────────
# Which pages each user may see. Created on first save, the same as the
# settings and user_preferences tables, so no migration step is needed.
# db/11_user_page_access.sql is the canonical schema.
#
# The '__configured__' key is not a page — see page_registry.CONFIGURED_MARKER.

def _ensure_user_page_access_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_page_access (
                username   VARCHAR(100) NOT NULL,
                page_key   VARCHAR(64)  NOT NULL,
                granted_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                granted_by VARCHAR(100),
                PRIMARY KEY (username, page_key)
            )
            """
        )
    conn.commit()


def load_all_page_access(conn) -> dict:
    """Returns {username: set(page_key)} for every user with stored rows.

    A user missing from the result has never been configured and should get the
    default pages; a user present with an empty-but-for-the-marker set has been
    configured to see nothing. Callers must keep those two apart — see
    core.get_user_pages.

    Only a missing table is treated as "nothing configured yet". Every other
    error is raised, deliberately: a connection failure must NOT look like an
    unconfigured user, because the fallback for an unconfigured user is to
    grant the default pages. Failing open on a database wobble would hand out
    access.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username, page_key FROM user_page_access")
            rows = cur.fetchall()
    except psycopg2.errors.UndefinedTable:
        conn.rollback()      # the failed statement poisons the transaction
        return {}
    out: dict = {}
    for username, page_key in rows:
        out.setdefault(username, set()).add(page_key)
    return out


def set_page_access_many(conn, access: dict, granted_by: str) -> None:
    """Replace the grants for several users in one transaction.
    access is {username: set(page_key)}. Either every user is saved or none is.

    Writes each user's configured marker in the SAME transaction as their
    grants — a marker without grants, or grants without a marker, both
    reintroduce the ambiguity the marker exists to remove."""
    from routers.page_registry import CONFIGURED_MARKER
    if not access:
        return
    _ensure_user_page_access_table(conn)
    try:
        with conn.cursor() as cur:
            rows = []
            for username, page_keys in access.items():
                cur.execute("DELETE FROM user_page_access WHERE username = %s", (username,))
                rows.extend((username, key, granted_by) for key in sorted(page_keys))
                rows.append((username, CONFIGURED_MARKER, granted_by))
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO user_page_access (username, page_key, granted_by) VALUES %s",
                rows,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def delete_user_and_access(conn, username: str) -> None:
    """Delete a user and their page access in one transaction.

    remove_user() commits on its own, so calling a separate clear afterwards
    can leave grants behind if the second statement fails — and a recreated
    username would then inherit the old access.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
            try:
                cur.execute("DELETE FROM user_page_access WHERE username = %s", (username,))
            except psycopg2.errors.UndefinedTable:
                # No access has ever been saved, so there is nothing to clear.
                # The user delete above is still wanted, so swallow this one
                # rather than losing the whole transaction.
                pass
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ── Page names ───────────────────────────────────────────────────────────────
# Names the admin has given the pages, set on the Settings > Page Names screen.
# A page with no row uses the label in page_registry. Lives in the PROD pricer
# DB, like the KPI layout, so every environment shows the same names.

def _ensure_page_names_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS page_names (
                page_key   VARCHAR(64)  PRIMARY KEY,
                label      VARCHAR(100) NOT NULL,
                updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_by VARCHAR(100)
            )
            """
        )
    conn.commit()


def load_page_names(conn) -> dict:
    """Returns {page_key: label} for every renamed page."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT page_key, label FROM page_names")
            return dict(cur.fetchall())
    except psycopg2.errors.UndefinedTable:
        conn.rollback()
        return {}


def save_page_names(conn, names: dict, updated_by: str) -> None:
    """Replace the stored names in one transaction. names is {page_key: label};
    a page left out goes back to its default name."""
    _ensure_page_names_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM page_names")
            if names:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO page_names (page_key, label, updated_by) VALUES %s",
                    [(key, label, updated_by) for key, label in names.items()],
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def get_prod_pricer_connection():
    """Connects to the prod pricer DB on prod and local; the dev DB on the deployed dev Lambda."""
    try:
        pool = _get_prod_pricer_pool()
    except UnauthorizedSSOTokenError:
        raise HTTPException(status_code=503, detail="AWS credentials expired — run: aws sso login --profile <your-profile>")
    except psycopg2.OperationalError as e:
        msg = next((l.strip() for l in str(e).splitlines() if l.strip()), str(e).strip())
        raise HTTPException(status_code=503, detail=f"Aurora connection failed — check VPN is connected. Detail: {msg}") from e
    connection = _pool_getconn_or_raise(pool, "prod_pricer")
    try:
        yield connection
    finally:
        pool.putconn(connection)


@contextmanager
def get_vehicle_database_connection():
    """Connection to the vehicle DB — prod cluster on the prod Lambda, dev cluster on the dev Lambda."""
    try:
        pool = _get_vehicle_pool()
    except UnauthorizedSSOTokenError:
        logger.error("AWS SSO session has expired. Run: aws sso login")
        raise HTTPException(status_code=503, detail="AWS SSO session has expired. Run: aws sso login")
    except psycopg2.OperationalError as e:
        msg = next((l.strip() for l in str(e).splitlines() if l.strip()), str(e).strip())
        raise HTTPException(status_code=503, detail=f"Database connection failed — check VPN/network connectivity. Detail: {msg}") from e

    connection = _pool_getconn_or_raise(pool, "vehicle")
    try:
        yield connection
    finally:
        pool.putconn(connection)


# ── 0% finance subsidy rates ──────────────────────────────────────────────────

def _ensure_subsidy_rate_tables(conn) -> None:
    """Create the subsidy rate tables if they are not there yet.

    Mirrors _ensure_user_preferences_table: db/06_subsidy_rates.sql is the
    canonical schema, but creating on demand means a fresh environment works
    without a manual migration step.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS subsidy_rate_schedules (
                id              SERIAL       PRIMARY KEY,
                finance_company VARCHAR(100) NOT NULL,
                finance_type    VARCHAR(10)  NOT NULL,
                effective_from  DATE         NOT NULL,
                created_by      VARCHAR(100),
                created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT subsidy_rate_schedules_type_chk CHECK (finance_type IN ('HP', 'PCP')),
                CONSTRAINT subsidy_rate_schedules_uniq UNIQUE (finance_company, finance_type, effective_from)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS subsidy_rate_benchmarks (
                schedule_id INTEGER      NOT NULL REFERENCES subsidy_rate_schedules(id) ON DELETE CASCADE,
                term_months SMALLINT     NOT NULL,
                rate_pct    NUMERIC(7,3) NOT NULL,
                PRIMARY KEY (schedule_id, term_months),
                CONSTRAINT subsidy_rate_benchmarks_term_chk CHECK (term_months BETWEEN 1 AND 120),
                CONSTRAINT subsidy_rate_benchmarks_rate_chk CHECK (rate_pct >= 0)
            )
            """
        )
    conn.commit()


def get_subsidy_schedules(conn) -> list:
    """All schedules, newest effective date first, each with its benchmark terms.

    Returns [{id, finance_company, finance_type, effective_from (date),
              benchmarks: [(term_months, Decimal rate_pct), ...]}]
    """
    _ensure_subsidy_rate_tables(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.finance_company, s.finance_type, s.effective_from,
                   b.term_months, b.rate_pct
            FROM subsidy_rate_schedules s
            LEFT JOIN subsidy_rate_benchmarks b ON b.schedule_id = s.id
            ORDER BY s.effective_from DESC, s.finance_company, s.finance_type, b.term_months
            """
        )
        rows = cur.fetchall()

    by_id: dict = {}
    order: list = []
    for sched_id, company, ftype, eff_from, term, rate in rows:
        if sched_id not in by_id:
            by_id[sched_id] = {
                "id": sched_id,
                "finance_company": company,
                "finance_type": ftype,
                "effective_from": eff_from,
                "benchmarks": [],
            }
            order.append(sched_id)
        if term is not None:
            by_id[sched_id]["benchmarks"].append((int(term), rate))
    return [by_id[i] for i in order]


def save_subsidy_schedule(conn, schedule_id, finance_company: str, finance_type: str,
                          effective_from, benchmarks: list, user: str = None) -> int:
    """Insert or update one schedule and replace its benchmark terms wholesale.

    benchmarks is [(term_months, rate_pct), ...]. Returns the schedule id.
    """
    _ensure_subsidy_rate_tables(conn)
    try:
        with conn.cursor() as cur:
            if schedule_id:
                cur.execute(
                    "UPDATE subsidy_rate_schedules "
                    "SET finance_company = %s, finance_type = %s, effective_from = %s, updated_at = NOW() "
                    "WHERE id = %s",
                    (finance_company, finance_type, effective_from, schedule_id),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"schedule {schedule_id} no longer exists")
                cur.execute("DELETE FROM subsidy_rate_benchmarks WHERE schedule_id = %s", (schedule_id,))
            else:
                cur.execute(
                    "INSERT INTO subsidy_rate_schedules (finance_company, finance_type, effective_from, created_by) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (finance_company, finance_type, effective_from, user),
                )
                schedule_id = cur.fetchone()[0]

            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO subsidy_rate_benchmarks (schedule_id, term_months, rate_pct) VALUES %s",
                [(schedule_id, t, r) for t, r in benchmarks],
            )
        conn.commit()
        return schedule_id
    except Exception:
        conn.rollback()
        raise


def delete_subsidy_schedule(conn, schedule_id: int) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM subsidy_rate_schedules WHERE id = %s", (schedule_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ── Chart targets ─────────────────────────────────────────────────────────────
# Per-date targets for the Google Ads Campaigns charts. db/12_chart_targets.sql
# is the canonical schema; the table is created on first save, the same as the
# settings and user_preferences tables, so no migration step is needed.
#
# Two metrics are stored per campaign (sessions and events). The conversion rate,
# the aggregate across campaigns and the All-events total are all derived at read
# time — see the schema file for why.

def _ensure_chart_targets_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chart_targets (
                chart_key    VARCHAR(200)  NOT NULL,
                target_date  DATE          NOT NULL,
                target_value NUMERIC(14,4) NOT NULL,
                created_by   VARCHAR(100),
                updated_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
                PRIMARY KEY (chart_key, target_date)
            )
            """
        )
    conn.commit()


def get_chart_targets(conn, chart_keys: list, start, end) -> dict:
    """Returns {chart_key: {date: float}} for the given keys over start..end.

    Tolerates the table not existing yet and returns nothing, so the page works
    before anyone has saved a target. Only a missing table is swallowed — every
    other error is raised, since silently returning no targets on a connection
    failure would look exactly like "no targets set" and hide the fault.
    """
    if not chart_keys:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chart_key, target_date, target_value
                FROM chart_targets
                WHERE chart_key = ANY(%s) AND target_date BETWEEN %s AND %s
                """,
                (list(chart_keys), start, end),
            )
            rows = cur.fetchall()
    except psycopg2.errors.UndefinedTable:
        conn.rollback()      # the failed statement poisons the transaction
        return {}
    out: dict = {}
    for chart_key, target_date, target_value in rows:
        out.setdefault(chart_key, {})[target_date] = float(target_value)
    return out


def get_chart_target_ranges(conn, chart_keys: list) -> dict:
    """Returns {chart_key: {'value', 'start', 'end'}} for the CURRENT target.

    The editor cannot read its state from the chart arrays: a target saved from
    today forward is invisible to a read bounded by the charts' last data date,
    which is often yesterday. This reads the stored target independently of what
    the charts show, so Add/Edit and the popup prefill are correct.

    "Current" is the latest run of dates still in force — the value on the last
    stored date, with the first and last dates holding that same value.
    """
    if not chart_keys:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                -- The current target is the value on the latest stored date,
                -- and the span of dates still holding that same value.
                -- Save writes one contiguous block from today to the horizon,
                -- so this does not need to reason about gaps: an earlier run at
                -- a different value simply stops the MIN at the changeover.
                WITH latest AS (
                    SELECT DISTINCT ON (chart_key) chart_key, target_value, target_date
                    FROM chart_targets
                    WHERE chart_key = ANY(%s)
                    ORDER BY chart_key, target_date DESC
                )
                SELECT l.chart_key, l.target_value,
                       MIN(c.target_date), MAX(c.target_date)
                FROM latest l
                JOIN chart_targets c
                  ON c.chart_key = l.chart_key
                 AND c.target_value = l.target_value
                GROUP BY l.chart_key, l.target_value
                """,
                (list(chart_keys),),
            )
            rows = cur.fetchall()
    except psycopg2.errors.UndefinedTable:
        conn.rollback()
        return {}
    return {
        chart_key: {'value': float(value), 'start': start, 'end': end}
        for chart_key, value, start, end in rows
    }


def save_chart_targets(conn, writes: list, start, end, user: str = None) -> None:
    """Write one or more targets across start..end in a SINGLE transaction.

    writes is [(chart_key, value), ...]. The conversion popup saves a sessions
    target and an events target together, and they must not half-apply: one
    committed without the other leaves a derived conversion rate that was never
    entered.
    """
    if not writes:
        return
    _ensure_chart_targets_table(conn)
    try:
        days = (end - start).days + 1
        rows = [
            (chart_key, start + datetime.timedelta(days=offset), value, user)
            for chart_key, value in writes
            for offset in range(days)
        ]
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO chart_targets (chart_key, target_date, target_value, created_by) VALUES %s "
                "ON CONFLICT (chart_key, target_date) DO UPDATE "
                "SET target_value = EXCLUDED.target_value, "
                "    created_by   = EXCLUDED.created_by, "
                "    updated_at   = NOW()",
                rows,
                page_size=1000,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def delete_chart_target(conn, chart_key: str, from_date) -> int:
    """Remove a target from from_date onward, leaving earlier dates in place.

    Mirrors save_chart_targets, which writes today forward: past days keep the
    target they were judged against at the time, so old charts still show what
    was being aimed at then.

    Returns the number of rows removed. A missing table means there was nothing
    to remove, which is not an error — but any other failure is raised rather
    than reported to the user as a successful removal.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chart_targets WHERE chart_key = %s AND target_date >= %s",
                (chart_key, from_date),
            )
            removed = cur.rowcount
        conn.commit()
        return removed
    except psycopg2.errors.UndefinedTable:
        conn.rollback()
        return 0
    except Exception:
        conn.rollback()
        raise


def upsert_chart_target_dates(conn, chart_key: str, rows: list, user: str = None) -> None:
    """Write per-date targets for one key: [(date, value), ...].

    Used by the KPI CSV import. Only the dates given are touched — a date already
    stored is overwritten, a new one is added, and every other date is left as
    it was, so a later file can carry amendments and new dates together.
    """
    if not rows:
        return
    _ensure_chart_targets_table(conn)
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO chart_targets (chart_key, target_date, target_value, created_by) VALUES %s "
                "ON CONFLICT (chart_key, target_date) DO UPDATE "
                "SET target_value = EXCLUDED.target_value, "
                "    created_by   = EXCLUDED.created_by, "
                "    updated_at   = NOW()",
                [(chart_key, d, value, user) for d, value in rows],
                page_size=1000,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ── Google Ads Campaigns: chosen campaigns ────────────────────────────────────
# Which GA4 campaign IDs the Google Ads Campaigns page shows. db/14_ga4_campaigns.sql
# is the schema; the table is created on first use. Removing a campaign leaves
# its chart_targets rows alone, so its targets return if it is added again.

def _ensure_ga4_campaigns_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ga4_campaigns (
                campaign_id VARCHAR(50)  NOT NULL PRIMARY KEY,
                added_by    VARCHAR(100),
                added_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
            """
        )
    conn.commit()


def get_ga4_campaign_ids(conn) -> list:
    """Chosen campaign IDs, in the order they were added. Empty before the
    table exists."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT campaign_id FROM ga4_campaigns ORDER BY added_at, campaign_id")
            return [row[0] for row in cur.fetchall()]
    except psycopg2.errors.UndefinedTable:
        conn.rollback()
        return []


def add_ga4_campaign(conn, campaign_id: str, user: str = None) -> None:
    _ensure_ga4_campaigns_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ga4_campaigns (campaign_id, added_by) VALUES (%s, %s) "
                "ON CONFLICT (campaign_id) DO NOTHING",
                (campaign_id, user),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def remove_ga4_campaign(conn, campaign_id: str) -> int:
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ga4_campaigns WHERE campaign_id = %s", (campaign_id,))
            removed = cur.rowcount
        conn.commit()
        return removed
    except psycopg2.errors.UndefinedTable:
        conn.rollback()
        return 0
    except Exception:
        conn.rollback()
        raise


def get_chart_target_summary(conn, chart_key: str) -> dict | None:
    """Returns {'count', 'first', 'last'} for the dates stored under one key, or
    None when nothing is stored (or the table does not exist yet)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), MIN(target_date), MAX(target_date) FROM chart_targets WHERE chart_key = %s",
                (chart_key,),
            )
            count, first, last = cur.fetchone()
    except psycopg2.errors.UndefinedTable:
        conn.rollback()
        return None
    if not count:
        return None
    return {'count': count, 'first': first, 'last': last}
