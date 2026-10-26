import datetime
import hashlib
import json
import logging
import os
import psycopg2
import psycopg2.extras
import psycopg2.pool
import secrets
from contextlib import contextmanager
from botocore.exceptions import UnauthorizedSSOTokenError
from fastapi import HTTPException
from utils.secrets_manager_client import get_secret
from utils.ddl_once import once_per_database

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
    # Logs page (routers/logs.py): days kept, and rows per page.
    'logs_retention_days':              30,
    'logs_page_size':                   100,
    'logs_slow_request_ms':             1000,
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
    # Settings > AI: Ask AI on or off per page, and the Cortex model every page uses.
    'kpi_ai_summary':                   '0',
    'orders_deliveries_ai':             '0',
    'cortex_model':                     'claude-sonnet-5',
    # Models offered in the Settings > AI dropdown, comma separated. Filled by
    # the Update Available Models button, which asks Snowflake what it serves.
    'cortex_models':                    'claude-sonnet-5',
    'mot_workers':                      10,
    # Settings > Email Attachments: skip inline / signature images (1 = yes),
    # but keep any inline image bigger than this many KB.
    'email_attach_skip_inline':         1,
    'email_attach_inline_keep_kb':      100,
    'valuation_cache_hours':            36,
    # How long Buying Requirements reuses data before refetching. Minutes.
    # 0 on the Snowflake setting means "follow the dbt schedule only".
    'sharepoint_cache_minutes':         30,
    'snowflake_cache_minutes':          0,
    'cap_override_enabled':             '0',
    'cap_override_subscriber_id':       '',
    'cap_override_password':            '',
    # Live users circles beside the Home button (Settings > Live Users).
    'presence_enabled':                 1,
    'presence_interval_seconds':        20,
    'presence_drop_off_seconds':        150,
    'presence_circle_size':             26,
    # Set-password links (Settings > User Accounts > Password Links): hours a
    # link works for, and the address each environment's links start with.
    'password_link_hours':              48,
    'password_link_base_prod':          'https://uk-vle.aramis-group.eu/dev',
    'password_link_base_dev':           'https://ceywu383w7.execute-api.us-east-2.amazonaws.com/dev',
    # The email the Users page opens in Outlook. Placeholders: {first_name}, {link}, {expires}.
    # The sign-off name is Outlook's own default signature, added when the email opens.
    'password_email_subject_activate':  'Your Vehicle List Evaluator Account',
    'password_email_body_activate': (
        'Hi {first_name},\n\n'
        'Your Vehicle List Evaluator account has been created. Please use the link below to set your password:\n\n'
        '{link}\n\n'
        'The link works once and expires on {expires}.\n\n'
        'If the link does not work, reply to this email and I will send you a new one.\n\n'
        'Kind regards,\n'
    ),
    'password_email_subject_reset':     'Reset Your Vehicle List Evaluator Password',
    'password_email_body_reset': (
        'Hi {first_name},\n\n'
        'Please use the link below to set a new password:\n\n'
        '{link}\n\n'
        'The link works once and expires on {expires}.\n\n'
        'If the link does not work, reply to this email and I will send you a new one.\n\n'
        'Kind regards,\n'
    ),
    'presence_max_circles':             5,
    'presence_border_width':            2,
    'presence_gap':                     4,
    # Outer ring in each person's own colour, as on Microsoft 365 (0 = no ring).
    'presence_ring_width':              1,
    'presence_ring_gap':                1,
    'presence_border_colour':           '#ffffff',
    'presence_text_colour':             '#ffffff',
    'presence_more_colour':             '#64748b',
    # Text size in the menu under the email, top right. Pixels.
    'account_menu_font_size':           14,
    # The page menu opened from the burger at the top left. Pixels.
    'side_menu_font_size':              15,
    # Text on the Settings and Users pages. Pixels.
    'settings_font_size':               14,
    'side_menu_width':                  260,
    # The header row at the top of every page (page_header). Pixels.
    'page_title_size':                  22,
    'page_menu_btn_size':               40,
    'page_head_pad_y':                  14,
    'page_head_pad_x':                  20,
    'page_head_gap':                    14,
    # Settings > Appearance: colours ('#rrggbb', see APPEARANCE_COLOUR_SETTINGS),
    # opacities (percent, 100 = solid), corner rounding and header shadow.
    'ap_page_bg':                       '#f1f5f9',
    'ap_surface':                       '#ffffff',
    'ap_header_bg':                     '#ffffff',
    'ap_menu_bg':                       '#ffffff',
    'ap_text':                          '#343a40',
    'ap_heading':                       '#1e3a5f',
    'ap_muted':                         '#6c757d',
    'ap_border':                        '#b0b0b0',
    'ap_menu_text':                     '#343a40',
    'ap_primary':                       '#0d6efd',
    'ap_primary_hover':                 '#0b5ed7',
    'ap_table_header_bg':               '#dbeafe',
    'ap_table_header_text':             '#1e3a5f',
    'ap_surface_opacity':               100,
    'ap_header_opacity':                100,
    'ap_menu_opacity':                  100,
    'ap_radius':                        8,
    'ap_header_shadow':                 6,
    # Settings > Appearance > Home Page: sizes in pixels, colours '#rrggbb'.
    'home_page_bg':                     '#003c57',
    'home_title_colour':                '#ffffff',
    'home_btn_bg':                      '#7ed321',
    'home_btn_hover_bg':                '#6cba18',
    'home_btn_text':                    '#0d3d5f',
    'home_title_size':                  32,
    'home_title_gap':                   48,
    'home_logo_size':                   80,
    'home_logo_radius':                 16,
    'home_logo_gap':                    20,
    'home_btn_width':                   280,
    'home_btn_height':                  80,
    'home_btn_font_size':               18,
    'home_btn_font_weight':             600,
    'home_btn_radius':                  8,
    # 3D look of the Home buttons: strength 0 = flat; edge = how dark the bottom edge is (percent black).
    'home_btn_3d':                      0,
    'home_btn_3d_edge':                 35,
    'home_grid_gap':                    20,
    'home_grid_max_width':              600,
    'home_pad_top':                     40,
    'home_pad_side':                    16,
    'home_pad_bottom':                  8,
    # Settings > User Activity: a session with no request for this long ends as Timed Out.
    'session_timeout_minutes':          30,
    # Force Re-login: whole epoch seconds. Logins before this are signed out on every server.
    'sessions_invalidated_at':          0,
}

BOOL_SETTINGS = {'calc_buying_reqs', 'get_stock', 'enable_cars_bought', 'kpi_ai_summary', 'orders_deliveries_ai', 'cap_override_enabled'}

# Live users colours, stored as '#rrggbb' text.
PRESENCE_COLOUR_SETTINGS = {'presence_border_colour', 'presence_text_colour', 'presence_more_colour'}

# Settings > Appearance colours, stored as '#rrggbb' text.
APPEARANCE_COLOUR_SETTINGS = {
    'ap_page_bg', 'ap_surface', 'ap_header_bg', 'ap_menu_bg', 'ap_text', 'ap_heading',
    'ap_muted', 'ap_border', 'ap_menu_text', 'ap_primary', 'ap_primary_hover',
    'ap_table_header_bg', 'ap_table_header_text',
    'home_page_bg', 'home_title_colour', 'home_btn_bg', 'home_btn_hover_bg', 'home_btn_text',
}

# Per-user bool settings stored as e.g. calc_buying_reqs__buyer, get_stock__buyer.
PER_USER_BOOL_SETTINGS = {'calc_buying_reqs', 'get_stock'}

ADMIN_USER = 'admin'
ALWAYS_ENABLED_USERS = {'admin', 'tdrayson'}

# The admin login comes from core._ADMIN_FALLBACK and may have no row in the
# users table, so its name and email cannot be saved there. Fixed here instead,
# and laid over whatever is stored wherever user details are read.
ADMIN_DETAILS = {
    'tdrayson': {'first_name': 'Tom', 'last_name': 'Drayson', 'email': 'tom.drayson@carsupermarket.com'},
}


PASSWORD_EMAIL_KEYS = ('password_email_subject_activate', 'password_email_body_activate',
                       'password_email_subject_reset', 'password_email_body_reset')


def widen_settings_values(conn) -> None:
    """settings.value starts as VARCHAR(250), too short for an email body.
    Changed to TEXT once; does nothing when it already is."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data_type FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'settings' AND column_name = 'value'"
        )
        row = cur.fetchone()
        if row is not None and row[0] != 'text':
            cur.execute("ALTER TABLE settings ALTER COLUMN value TYPE TEXT")
    conn.commit()


def get_settings(conn, user: str = None) -> dict:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM settings")
            rows = cur.fetchall()
        row_map = dict(rows)
        result = dict(SETTINGS_DEFAULTS)
        STRING_SETTINGS = {'cap_override_subscriber_id', 'cap_override_password', 'cortex_model', 'cortex_models',
                           'email_sender', 'email_test_recipient', 'login_mode',
                           'password_link_base_prod', 'password_link_base_dev'} | set(PASSWORD_EMAIL_KEYS) | PRESENCE_COLOUR_SETTINGS | APPEARANCE_COLOUR_SETTINGS
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
        result['orders_deliveries_ai'] = row_map.get('orders_deliveries_ai', '0') == '1'
        result['cortex_model'] = row_map.get('cortex_model') or SETTINGS_DEFAULTS['cortex_model']
        models = [m for m in (row_map.get('cortex_models') or '').split(',') if m.strip()]
        if result['cortex_model'] not in models:
            models.append(result['cortex_model'])
        result['cortex_models'] = models
        result['cap_override_enabled'] = row_map.get('cap_override_enabled', '0') == '1'
        result['cap_override_subscriber_id'] = row_map.get('cap_override_subscriber_id', '')
        result['cap_override_password'] = row_map.get('cap_override_password', '')
        result['email_sender'] = row_map.get('email_sender', '')
        result['email_test_recipient'] = row_map.get('email_test_recipient', '')
        # 'both' = username or email address at login, 'email' = email address only.
        result['login_mode'] = row_map.get('login_mode', 'both')
        for key in ('password_link_base_prod', 'password_link_base_dev') + PASSWORD_EMAIL_KEYS:
            result[key] = row_map.get(key) or SETTINGS_DEFAULTS[key]
        for key in PRESENCE_COLOUR_SETTINGS | APPEARANCE_COLOUR_SETTINGS:
            result[key] = row_map.get(key, SETTINGS_DEFAULTS[key])
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
        d['orders_deliveries_ai'] = False
        d['cap_override_enabled'] = False
        d['cap_override_subscriber_id'] = ''
        d['cap_override_password'] = ''
        d['email_sender'] = ''
        d['email_test_recipient'] = ''
        d['login_mode'] = 'both'
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
        # One statement for all keys: a statement per key is one network round
        # trip each, which is slow on a laptop reaching the database over VPN.
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO settings (key, value) VALUES %s "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            [(key, str(value)) for key, value in values.items()]
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

@once_per_database
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

# One row per session. A session starts at a sign-in ('password'), when a
# login cookie with no row is first seen ('cookie': signed in before this
# tracking began, or the sign-in insert failed), or when someone comes back
# on the same cookie after the timeout ('returned'). It ends once, with one of
# SESSION_END_REASONS, and an ended row is never changed again: every update
# below is guarded by "ended_at IS NULL", and anything that ends sessions
# first closes the ones already past the timeout, so a session left hours
# ago is never relabelled Forced or Logout. db/18_user_session_end.sql is
# the same change for running by hand.
#
# All times are TIMESTAMPTZ written with the database's NOW(); pages convert
# to UK time with AT TIME ZONE 'Europe/London'.

SESSION_END_REASONS = ('logout', 'timeout', 'forced', 'password_changed', 'user_deleted')

_user_sessions_ready = False
_SESSIONS_MIGRATION_LOCK = 724180001   # any fixed number, used only here


def _ensure_user_sessions(conn) -> None:
    """Runs once per process. Looks at information_schema first so the
    ALTERs (which lock the table) only run when something is missing. The
    advisory lock stops two containers starting together from converting
    the time columns twice."""
    global _user_sessions_ready
    if _user_sessions_ready:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
                session_id   VARCHAR(64)  PRIMARY KEY,
                username     VARCHAR(100) NOT NULL,
                environment  VARCHAR(10)  NOT NULL,
                login_at     TIMESTAMPTZ  NOT NULL,
                last_seen_at TIMESTAMPTZ  NOT NULL,
                logout_at    TIMESTAMPTZ  NULL
            )
            """
        )
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'user_sessions'"
        )
        columns = dict(cur.fetchall())
        if 'end_reason' not in columns or columns.get('login_at') == 'timestamp without time zone':
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SESSIONS_MIGRATION_LOCK,))
            # Read again under the lock: another container may have just done it.
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'user_sessions'"
            )
            columns = dict(cur.fetchall())
            if columns.get('login_at') == 'timestamp without time zone':
                # login_at and logout_at were written in UK time. last_seen_at was
                # UK time where it was only set at login or logout, and database
                # time where record_event moved it on.
                cur.execute(
                    """
                    ALTER TABLE user_sessions
                        ALTER COLUMN last_seen_at TYPE TIMESTAMPTZ USING
                            CASE WHEN last_seen_at = login_at OR last_seen_at = logout_at
                                 THEN last_seen_at AT TIME ZONE 'Europe/London'
                                 ELSE last_seen_at AT TIME ZONE current_setting('TimeZone') END,
                        ALTER COLUMN login_at  TYPE TIMESTAMPTZ USING login_at  AT TIME ZONE 'Europe/London',
                        ALTER COLUMN logout_at TYPE TIMESTAMPTZ USING logout_at AT TIME ZONE 'Europe/London'
                    """
                )
                cur.execute("UPDATE user_sessions SET last_seen_at = login_at WHERE last_seen_at < login_at")
            if 'end_reason' not in columns:
                cur.execute(
                    "ALTER TABLE user_sessions "
                    "ADD COLUMN IF NOT EXISTS started_by VARCHAR(20) NOT NULL DEFAULT 'password', "
                    "ADD COLUMN IF NOT EXISTS ended_at   TIMESTAMPTZ NULL, "
                    "ADD COLUMN IF NOT EXISTS end_reason VARCHAR(20) NULL"
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_last_seen ON user_sessions (last_seen_at)")
                cur.execute(
                    "UPDATE user_sessions SET ended_at = logout_at, end_reason = 'logout' "
                    "WHERE logout_at IS NOT NULL AND ended_at IS NULL"
                )
    conn.commit()
    _user_sessions_ready = True


def new_session_id() -> str:
    return hashlib.sha256(os.urandom(16)).hexdigest()


def next_session_id(session_id: str) -> str:
    """The session that follows this one after a timeout. Fixed rather than
    random, so two requests crossing the timeout together both land on the
    same new row instead of making one each."""
    return hashlib.sha256(f"{session_id}:next".encode()).hexdigest()


def _close_timed_out(cur, timeout_minutes: int, username: str | None = None,
                     session_id: str | None = None) -> None:
    """End open sessions with no request for timeout_minutes. They end when
    the timeout ran out, not at the moment this happens to run."""
    cur.execute(
        """
        UPDATE user_sessions
        SET ended_at = last_seen_at + INTERVAL '1 minute' * %s, end_reason = 'timeout'
        WHERE ended_at IS NULL
          AND last_seen_at < NOW() - INTERVAL '1 minute' * %s
          AND (%s::text IS NULL OR username = %s)
          AND (%s::text IS NULL OR session_id = %s)
        """,
        (timeout_minutes, timeout_minutes, username, username, session_id, session_id),
    )


def record_login(conn, session_id: str, username: str, environment: str) -> None:
    _ensure_user_sessions(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_sessions (session_id, username, environment, login_at, last_seen_at, started_by)
            VALUES (%s, %s, %s, NOW(), NOW(), 'password')
            ON CONFLICT (session_id) DO NOTHING
            """,
            (session_id, username, environment),
        )
    conn.commit()


def record_logout(conn, session_id: str, timeout_minutes: int) -> None:
    """A logout after the timeout has already run out leaves it as Timed Out."""
    _ensure_user_sessions(conn)
    with conn.cursor() as cur:
        _close_timed_out(cur, timeout_minutes, session_id=session_id)
        cur.execute(
            "UPDATE user_sessions SET ended_at = NOW(), logout_at = NOW(), last_seen_at = NOW(), "
            "end_reason = 'logout' WHERE session_id = %s AND ended_at IS NULL",
            (session_id,),
        )
    conn.commit()


def close_sessions(conn, reason: str, timeout_minutes: int, username: str | None = None,
                   session_id: str | None = None, ensure: bool = True) -> int:
    """End every open session (or one user's, or one session) with reason.
    Returns how many were ended with that reason. ensure=False is for another
    environment's database, already checked with user_sessions_ready: the
    table is not created or converted there."""
    if reason not in SESSION_END_REASONS:
        raise ValueError(f"Unknown session end reason: {reason}")
    if ensure:
        _ensure_user_sessions(conn)
    with conn.cursor() as cur:
        _close_timed_out(cur, timeout_minutes, username=username, session_id=session_id)
        cur.execute(
            """
            UPDATE user_sessions SET ended_at = NOW(), end_reason = %s
            WHERE ended_at IS NULL
              AND (%s::text IS NULL OR username = %s)
              AND (%s::text IS NULL OR session_id = %s)
            """,
            (reason, username, username, session_id, session_id),
        )
        count = cur.rowcount
    conn.commit()
    return count


def touch_session(conn, session_id: str, username: str, environment: str, timeout_minutes: int) -> str | None:
    """Record that this login is still in use. Returns the session id the
    cookie should hold from now on, or None when the login has ended
    (logout, forced, password changed, user deleted) and must be signed out.

    - open and within the timeout: last_seen_at moves on, same id
    - no row (old cookie, or a failed sign-in insert): a 'cookie' row is made
    - past the timeout: closed as 'timeout', and the next id in the chain is
      used ('returned'). A cookie carrying an older id (a late response can
      put one back) follows the chain to the open row."""
    _ensure_user_sessions(conn)
    current = session_id
    via_timeout = False
    try:
        with conn.cursor() as cur:
            for _ in range(20):
                cur.execute(
                    """
                    UPDATE user_sessions SET last_seen_at = NOW()
                    WHERE session_id = %s AND username = %s AND ended_at IS NULL
                      AND last_seen_at >= NOW() - INTERVAL '1 minute' * %s
                    RETURNING 1
                    """,
                    (current, username, timeout_minutes),
                )
                if cur.fetchone():
                    conn.commit()
                    return current
                cur.execute(
                    "SELECT username, ended_at IS NULL, end_reason FROM user_sessions WHERE session_id = %s",
                    (current,),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                        INSERT INTO user_sessions (session_id, username, environment, login_at, last_seen_at, started_by)
                        VALUES (%s, %s, %s, NOW(), NOW(), %s)
                        ON CONFLICT (session_id) DO NOTHING
                        RETURNING 1
                        """,
                        (current, username, environment, 'returned' if via_timeout else 'cookie'),
                    )
                    if cur.fetchone():
                        conn.commit()
                        return current
                    continue   # another request inserted it a moment ago: go round and use it
                owner, is_open, reason = row
                if owner != username:
                    conn.commit()
                    return None
                if is_open:
                    _close_timed_out(cur, timeout_minutes, session_id=current)
                elif reason != 'timeout':
                    conn.commit()
                    return None
                current = next_session_id(current)
                via_timeout = True
        conn.commit()
        raise RuntimeError(f"Session chain too long from {session_id}")
    except Exception:
        conn.rollback()
        raise


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
        # last_seen_at is kept up to date by touch_session on every request.
    conn.commit()


def read_session_settings(conn) -> dict:
    """session_timeout_minutes and sessions_invalidated_at, straight from the
    settings table. Unlike get_settings this RAISES on a failed read, so the
    caller can keep the last value it had: falling back to the default 0
    would undo a Force Re-login during a database blip."""
    keys = ('session_timeout_minutes', 'sessions_invalidated_at')
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM settings WHERE key IN %s", (keys,))
        stored = dict(cur.fetchall())
    out = {}
    for key in keys:
        try:
            out[key] = int(stored.get(key, SETTINGS_DEFAULTS[key]))
        except (TypeError, ValueError):
            out[key] = SETTINGS_DEFAULTS[key]
    return out


def get_last_login_per_user(conn, write: bool = True) -> dict:
    """{username: {'last_login', 'last_seen'}} as UK time strings. last_login
    is the latest session start of any kind, so a login cookie carried over
    from before this tracking began still counts. write=False is for another
    environment's database: nothing is created or changed there."""
    uk_time = " AT TIME ZONE 'Europe/London'"
    if write:
        _ensure_user_sessions(conn)
    else:
        # Another environment's table may not have been converted yet. Before
        # that, login_at and last_seen_at are plain timestamps already in UK
        # time (last_seen_at can be an hour out where the database clock set
        # it), so they are read as they are.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'user_sessions' "
                "AND column_name = 'login_at'"
            )
            found = cur.fetchone()
        conn.commit()
        if not found:
            return {}
        if found[0] == 'timestamp without time zone':
            uk_time = ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT username,
                   MAX(login_at){uk_time},
                   MAX(last_seen_at){uk_time}
            FROM user_sessions
            GROUP BY username
            """
        )
        return {
            row[0]: {'last_login': row[1].strftime('%Y-%m-%d %H:%M'),
                     'last_seen': row[2].strftime('%Y-%m-%d %H:%M')}
            for row in cur.fetchall()
        }


def user_sessions_ready(conn) -> bool:
    """True when this database's user_sessions has the session-end columns and
    TIMESTAMPTZ times, i.e. code with this change has run against it. Used
    before reading another environment's sessions, where nothing may be changed."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'user_sessions'"
        )
        columns = dict(cur.fetchall())
    conn.commit()
    return 'end_reason' in columns and columns.get('login_at') == 'timestamp with time zone'


# Sessions past the timeout that nothing has closed yet count as Timed Out at
# last_seen_at + timeout. The same as _close_timed_out would store, so a
# read-only view (another environment's database) shows what it will become.
_EFFECTIVE_SESSIONS_SQL = """
    SELECT session_id, username, environment, login_at, last_seen_at, started_by,
           CASE WHEN ended_at IS NULL AND last_seen_at < NOW() - INTERVAL '1 minute' * %(timeout)s
                THEN last_seen_at + INTERVAL '1 minute' * %(timeout)s ELSE ended_at END AS ended_at,
           CASE WHEN ended_at IS NULL AND last_seen_at < NOW() - INTERVAL '1 minute' * %(timeout)s
                THEN 'timeout' ELSE end_reason END AS end_reason
    FROM user_sessions
"""


def get_active_sessions(conn, timeout_minutes: int, write: bool = True) -> list:
    """Open sessions, most recent first. write=False reads without changing
    anything (for another environment's database)."""
    if write:
        _ensure_user_sessions(conn)
    with conn.cursor() as cur:
        if write:
            _close_timed_out(cur, timeout_minutes)
        cur.execute(
            f"""
            WITH s AS ({_EFFECTIVE_SESSIONS_SQL})
            SELECT username, environment, login_at AT TIME ZONE 'Europe/London',
                   last_seen_at AT TIME ZONE 'Europe/London', started_by
            FROM s
            WHERE ended_at IS NULL
            ORDER BY last_seen_at DESC
            """,
            {'timeout': timeout_minutes},
        )
        rows = cur.fetchall()
    conn.commit()
    return [
        {'user': r[0], 'environment': r[1], 'login_time': r[2].strftime('%Y-%m-%d %H:%M'),
         'last_seen': r[3].strftime('%Y-%m-%d %H:%M'), 'started_by': r[4]}
        for r in rows
    ]


SESSION_LIST_LIMIT = 5000


def get_sessions(conn, timeout_minutes: int, date_from: datetime.date, date_to: datetime.date,
                 username: str | None = None, environment: str | None = None,
                 ended_by: str | None = None, write: bool = True,
                 limit: int = SESSION_LIST_LIMIT) -> list:
    """Sessions started between date_from and date_to inclusive (UK dates),
    newest first, at most SESSION_LIST_LIMIT. ended_by is one of
    SESSION_END_REASONS, or 'active' for sessions still open. With write,
    closes timed-out sessions first so what is shown is stored history;
    write=False changes nothing (for another environment's database)."""
    if write:
        _ensure_user_sessions(conn)
    with conn.cursor() as cur:
        if write:
            _close_timed_out(cur, timeout_minutes)
        cur.execute(
            f"""
            WITH s AS ({_EFFECTIVE_SESSIONS_SQL})
            SELECT session_id, username, environment,
                   login_at     AT TIME ZONE 'Europe/London',
                   last_seen_at AT TIME ZONE 'Europe/London',
                   ended_at     AT TIME ZONE 'Europe/London',
                   end_reason, started_by,
                   EXTRACT(EPOCH FROM last_seen_at - login_at)::BIGINT
            FROM s
            WHERE login_at >= (%(date_from)s::date::timestamp AT TIME ZONE 'Europe/London')
              AND login_at <  ((%(date_to)s::date + 1)::timestamp AT TIME ZONE 'Europe/London')
              AND (%(username)s::text IS NULL OR username = %(username)s)
              AND (%(environment)s::text IS NULL OR environment = %(environment)s)
              AND (%(ended_by)s::text IS NULL
                   OR (%(ended_by)s = 'active' AND ended_at IS NULL)
                   OR end_reason = %(ended_by)s)
            ORDER BY login_at DESC
            LIMIT %(limit)s
            """,
            {'timeout': timeout_minutes, 'date_from': date_from, 'date_to': date_to,
             'username': username, 'environment': environment, 'ended_by': ended_by,
             'limit': limit},
        )
        rows = cur.fetchall()
    conn.commit()
    fmt = '%Y-%m-%d %H:%M'
    return [
        {
            'session_id': r[0], 'user': r[1], 'environment': r[2],
            'started': r[3].strftime(fmt), 'last_activity': r[4].strftime(fmt),
            'ended': r[5].strftime(fmt) if r[5] else '',
            'end_reason': r[6] or 'active', 'started_by': r[7] or 'password',
            'duration_seconds': max(0, r[8] or 0),
        }
        for r in rows
    ]


def get_session_environments(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT environment FROM user_sessions ORDER BY environment")
        out = [r[0] for r in cur.fetchall()]
    conn.commit()
    return out


def get_session_usernames(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT username FROM user_sessions ORDER BY username")
        out = [r[0] for r in cur.fetchall()]
    conn.commit()
    return out


# ── Live users on a page ─────────────────────────────────────────────────────
# One row per open browser tab (a new id on every page load), so two windows
# on one login on different pages do not overwrite each other. db/16_page_presence.sql
# is the same table for running by hand.

_page_presence_ready = False


def _ensure_page_presence_table(conn) -> None:
    """Runs once per process: the heartbeat is called every few seconds per
    tab, so the CREATE/ALTER statements must not run on every call."""
    global _page_presence_ready
    if _page_presence_ready:
        return
    _ensure_user_detail_columns(conn)   # the list below reads first/last name
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS page_presence (
                tab_id        VARCHAR(64)  PRIMARY KEY,
                session_id    VARCHAR(64)  NOT NULL,
                username      VARCHAR(100) NOT NULL,
                environment   VARCHAR(10)  NOT NULL,
                page          VARCHAR(200) NOT NULL,
                last_seen_at  TIMESTAMP    NOT NULL DEFAULT NOW()
            )
            """
        )
        # seq and left_tab came after the first version of the table.
        cur.execute(
            "ALTER TABLE page_presence "
            "ADD COLUMN IF NOT EXISTS seq      INTEGER NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS left_tab BOOLEAN NOT NULL DEFAULT FALSE"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_page_presence_page ON page_presence (environment, page)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_page_presence_last_seen ON page_presence (last_seen_at)")
    conn.commit()
    _page_presence_ready = True


def touch_page_presence(conn, tab_id: str, seq: int, session_id: str, username: str, environment: str,
                        page: str, drop_off_seconds: int, only_user: str | None = None,
                        all_pages: bool = False) -> list:
    """Record that this tab is on `page`, then return who else is on it.

    Returns [(username, first_name, last_name, pages)], one entry per person
    however many tabs they have open, seen within drop_off_seconds. pages is
    every page that person has open. only_user limits the list to that one
    person. all_pages lists people on any page, not just this one.

    seq goes up by one with every message a tab sends. A message only counts
    if its seq is higher than the last one stored, so a heartbeat that arrives
    after the tab's leave message cannot bring the tab back."""
    _ensure_page_presence_table(conn)
    with conn.cursor() as cur:
        # The username check stops a tab id sent by one user taking over another's row.
        # NOT EXISTS: a heartbeat sent with the old cookie after logout is ignored
        # (the session cookie itself cannot be withdrawn once sent).
        cur.execute(
            """
            INSERT INTO page_presence (tab_id, session_id, username, environment, page, last_seen_at, seq, left_tab)
            SELECT %s, %s, %s, %s, %s, NOW(), %s, FALSE
            WHERE NOT EXISTS (
                SELECT 1 FROM user_sessions WHERE session_id = %s AND logout_at IS NOT NULL
            )
            ON CONFLICT (tab_id) DO UPDATE
                SET page = EXCLUDED.page, last_seen_at = NOW(), seq = EXCLUDED.seq, left_tab = FALSE
                WHERE page_presence.username = EXCLUDED.username
                  AND EXCLUDED.seq > page_presence.seq
            """,
            (tab_id, session_id, username, environment, page, seq, session_id),
        )
        # Rows from tabs that closed without saying so.
        cur.execute("DELETE FROM page_presence WHERE last_seen_at < NOW() - INTERVAL '1 hour'")
        cur.execute(
            """
            SELECT p.username, MAX(u.first_name), MAX(u.last_name), ARRAY_AGG(DISTINCT p.page)
            FROM page_presence p
            LEFT JOIN users u ON u.username = p.username
            WHERE p.environment = %s
              AND (%s OR p.page = %s)
              AND NOT p.left_tab
              AND p.last_seen_at >= NOW() - (INTERVAL '1 second' * %s)
              AND (%s IS NULL OR p.username = %s)
            GROUP BY p.username
            ORDER BY p.username
            """,
            (environment, all_pages, page, drop_off_seconds, only_user, only_user),
        )
        rows = [
            (u, ADMIN_DETAILS[u]['first_name'], ADMIN_DETAILS[u]['last_name'], pages) if u in ADMIN_DETAILS
            else (u, f, l, pages)
            for u, f, l, pages in cur.fetchall()
        ]
    conn.commit()
    return rows


def mark_page_presence_left(conn, tab_id: str, seq: int, session_id: str, username: str,
                            environment: str) -> None:
    """The tab was closed, hidden or left: take it off straight away.

    Marks the row rather than deleting it, so its seq is kept and a late
    heartbeat with a lower seq is still ignored. Inserts a marked row if the
    leave arrives before the tab's first heartbeat. Same seq rule as
    touch_page_presence, so a late leave cannot remove a tab that came back."""
    _ensure_page_presence_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO page_presence (tab_id, session_id, username, environment, page, last_seen_at, seq, left_tab)
            VALUES (%s, %s, %s, %s, '', NOW(), %s, TRUE)
            ON CONFLICT (tab_id) DO UPDATE
                SET seq = EXCLUDED.seq, left_tab = TRUE
                WHERE page_presence.username = EXCLUDED.username
                  AND EXCLUDED.seq > page_presence.seq
            """,
            (tab_id, session_id, username, environment, seq),
        )
    conn.commit()


def remove_session_presence(conn, session_id: str) -> None:
    """Logout: every tab on that login. Marked with the highest possible seq
    rather than deleted, so no heartbeat still on its way can bring one back.
    The 1-hour cleanup in touch_page_presence deletes them later."""
    _ensure_page_presence_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE page_presence SET left_tab = TRUE, seq = 2147483647 WHERE session_id = %s",
            (session_id,),
        )
    conn.commit()


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


@once_per_database
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
    return read_user_details(conn)


def read_user_details(conn) -> dict:
    """load_user_details without the column check. The account menu reads this
    on every page, and ALTER TABLE locks the users table even when the columns
    already exist, holding up logins."""
    with conn.cursor() as cur:
        cur.execute("SELECT username, first_name, last_name, email FROM users")
        details = {
            row[0]: {'first_name': row[1] or '', 'last_name': row[2] or '', 'email': row[3] or ''}
            for row in cur.fetchall()
        }
    for username, fixed in ADMIN_DETAILS.items():
        details[username] = dict(fixed)
    return details


# ── Set-password links (account activation and password reset) ──────────────
# One table for both kinds of link: purpose 'activate' (new account with no
# password yet) or 'reset'. Only a hash of the token is stored, so a copy of the
# database cannot be used to open a link. db/19_password_links.sql is the same
# change for running by hand.

PASSWORD_LINK_PURPOSES = ('activate', 'reset')


def _password_links_present(conn) -> bool:
    """True when both the password_links table and users.activated exist.
    A plain read, so it never locks the users table."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT COUNT(*) FROM information_schema.tables "
            "        WHERE table_schema = current_schema() AND table_name = 'password_links'), "
            "       (SELECT COUNT(*) FROM information_schema.columns "
            "        WHERE table_schema = current_schema() AND table_name = 'users' "
            "          AND column_name = 'activated')"
        )
        tables, columns = cur.fetchone()
    return tables > 0 and columns > 0


@once_per_database
def _ensure_password_links(conn) -> None:
    """Create the table and the users.activated column on first use. Every
    existing user stays activated (the column defaults to TRUE). The ALTER only
    runs when the column is missing, because it locks the users table."""
    if _password_links_present(conn):
        return
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS activated BOOLEAN NOT NULL DEFAULT TRUE")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS password_links (
                token_hash   CHAR(64) PRIMARY KEY,
                username     VARCHAR(100) NOT NULL REFERENCES users (username) ON DELETE CASCADE,
                purpose      VARCHAR(10) NOT NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at   TIMESTAMPTZ NOT NULL,
                used_at      TIMESTAMPTZ NULL,
                cancelled_at TIMESTAMPTZ NULL,
                created_by   VARCHAR(100)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_password_links_username ON password_links (username)")
    conn.commit()


def _hash_link_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def read_inactive_users(conn) -> set:
    """Usernames that have not set a password yet. Empty when the activated
    column does not exist (every user counts as activated)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'users' AND column_name = 'activated'"
        )
        if cur.fetchone() is None:
            return set()
        cur.execute("SELECT username FROM users WHERE NOT activated")
        return {row[0] for row in cur.fetchall()}


def add_pending_user(conn, username: str) -> str:
    """A new user with no password. The stored hash is not a hex digest, so no
    password can ever match it; the activation link replaces it. Returns that
    stored value."""
    _ensure_password_links(conn)
    pwd_hash = '!' + secrets.token_hex(16)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, pwd_hash, activated) VALUES (%s, %s, FALSE)",
            (username, pwd_hash),
        )
    conn.commit()
    return pwd_hash


def mark_user_activated(conn, username: str) -> None:
    """A password set by hand also activates the account. Nothing to do when
    the column does not exist yet (everyone is already activated)."""
    if not _password_links_present(conn):
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET activated = TRUE WHERE username = %s", (username,))
    conn.commit()


def cancel_password_links(conn, username: str) -> int:
    """Cancel this user's unused links, so an old link cannot overwrite a
    password that was just set another way. Returns how many were cancelled."""
    if not _password_links_present(conn):
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE password_links SET cancelled_at = NOW() "
            "WHERE username = %s AND used_at IS NULL AND cancelled_at IS NULL",
            (username,),
        )
        count = cur.rowcount
    conn.commit()
    return count


def create_password_link(conn, username: str, purpose: str, hours: int, created_by: str) -> tuple:
    """New link for an existing user; any earlier unused link for them is
    cancelled in the same step. Returns (token, expires_at). The token is shown
    once and never stored."""
    if purpose not in PASSWORD_LINK_PURPOSES:
        raise ValueError(f"Unknown password link purpose: {purpose}")
    _ensure_password_links(conn)
    token = secrets.token_urlsafe(32)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE password_links SET cancelled_at = NOW() "
                "WHERE username = %s AND used_at IS NULL AND cancelled_at IS NULL",
                (username,),
            )
            cur.execute(
                "INSERT INTO password_links (token_hash, username, purpose, expires_at, created_by) "
                "VALUES (%s, %s, %s, NOW() + INTERVAL '1 hour' * %s, %s) RETURNING expires_at",
                (_hash_link_token(token), username, purpose, hours, created_by),
            )
            expires_at = cur.fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return token, expires_at


def peek_password_link(conn, token: str) -> tuple | None:
    """(username, purpose) when the link is unused, not cancelled and not
    expired, else None. Read only: opening the page must not use the link up,
    because email and chat programs open links to make a preview."""
    if not token or not _password_links_present(conn):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT username, purpose FROM password_links WHERE token_hash = %s "
            "AND used_at IS NULL AND cancelled_at IS NULL AND expires_at > NOW()",
            (_hash_link_token(token),),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else None


def redeem_password_link(conn, token: str, pwd_hash: str) -> tuple | None:
    """Use the link and save the password as one step. Returns (username,
    purpose), or None when the link is not valid (nothing is changed). If the
    password cannot be saved the link is left usable."""
    if not token or not _password_links_present(conn):
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE password_links SET used_at = NOW() WHERE token_hash = %s "
                "AND used_at IS NULL AND cancelled_at IS NULL AND expires_at > NOW() "
                "RETURNING username, purpose",
                (_hash_link_token(token),),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return None
            cur.execute(
                "UPDATE users SET pwd_hash = %s, activated = TRUE WHERE username = %s",
                (pwd_hash, row[0]),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return None
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return row[0], row[1]


def find_usernames_by_email(conn, email: str) -> list:
    """Usernames with this email (case-insensitive). More than one when
    accounts share an address."""
    _ensure_user_detail_columns(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT username FROM users WHERE LOWER(email) = LOWER(%s)", (email.strip(),))
        usernames = [row[0] for row in cur.fetchall()]
    for username, fixed in ADMIN_DETAILS.items():
        if fixed['email'].lower() == email.strip().lower() and username not in usernames:
            usernames.append(username)
    return usernames


def update_user_details(conn, details: dict, ensure: bool = True) -> None:
    """Save name and email for several users in one transaction.
    details is {username: {'first_name', 'last_name', 'email'}}; empty strings
    are stored as NULL. ensure=False skips the ALTER TABLE, for another
    environment's database that already has the columns."""
    if ensure:
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

@once_per_database
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
            # A failed statement cancels the whole transaction in Postgres, so
            # the missing-table case steps back to this savepoint; otherwise
            # the commit below would silently undo the user delete too.
            cur.execute("SAVEPOINT clear_page_access")
            try:
                cur.execute("DELETE FROM user_page_access WHERE username = %s", (username,))
            except psycopg2.errors.UndefinedTable:
                # No access has ever been saved, so there is nothing to clear.
                cur.execute("ROLLBACK TO SAVEPOINT clear_page_access")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ── Page names ───────────────────────────────────────────────────────────────
# Names the admin has given the pages, set on the Settings > Page Names screen.
# A page with no row uses the label in page_registry. Lives in the PROD pricer
# DB, like the KPI layout, so every environment shows the same names.

@once_per_database
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


_prod_activity_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_prod_activity_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """The prod pricer DB from every environment, for Settings > User Activity
    only. Unlike _get_prod_pricer_pool this reaches prod from the deployed dev
    Lambda too (it can connect to the prod cluster), without moving the KPI
    layout and page names off the dev DB there. Small and with a short connect
    timeout, so an unreachable prod fails the page quickly instead of hanging."""
    if _is_prod_lambda():
        return _get_pool()
    if not os.getenv('AWS_LAMBDA_FUNCTION_NAME'):
        return _get_prod_pricer_pool()     # local: already points at prod
    global _prod_activity_pool
    if _prod_activity_pool is None:
        data = get_secret('prod/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials')
        _prod_activity_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=0, maxconn=2,
            host=data['endpoint'], user=data['username'], password=data['password'],
            database='carsupermarket_pricer', port=int(data.get('port', 5432)),
            connect_timeout=5, **_KEEPALIVE_KWARGS,
        )
    return _prod_activity_pool


@contextmanager
def get_prod_activity_connection():
    """Read prod's user sessions from any environment. Callers must not write."""
    try:
        pool = _get_prod_activity_pool()
    except UnauthorizedSSOTokenError:
        raise HTTPException(status_code=503, detail="AWS credentials expired — run: aws sso login --profile <your-profile>")
    except psycopg2.OperationalError as e:
        msg = next((l.strip() for l in str(e).splitlines() if l.strip()), str(e).strip())
        raise HTTPException(status_code=503, detail=f"Could not reach the prod database. Detail: {msg}") from e
    connection = _pool_getconn_or_raise(pool, "prod_activity")
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

@once_per_database
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

@once_per_database
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


def replace_chart_target_dates(conn, chart_key: str, rows: list, user: str = None) -> None:
    """Replace every per-date target for one key with rows: [(date, value), ...].

    Used by the KPI page's pasted targets: the box shows the whole list, so
    what is saved is the whole list — a date taken out of the box is removed.
    The delete and the insert are one transaction, so a failed save leaves the
    old list as it was. Empty rows removes them all.
    """
    _ensure_chart_targets_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chart_targets WHERE chart_key = %s", (chart_key,))
            if rows:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO chart_targets (chart_key, target_date, target_value, created_by) VALUES %s",
                    [(chart_key, d, value, user) for d, value in rows],
                    page_size=1000,
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_chart_target_overview(conn, key_prefix: str) -> list:
    """Every stored key starting with key_prefix, with what it holds:
    [(chart_key, count, first_date, last_date, min_value, max_value)].
    Tolerates the table not existing yet, like get_chart_targets."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chart_key, COUNT(*), MIN(target_date), MAX(target_date),
                       MIN(target_value), MAX(target_value)
                FROM chart_targets
                WHERE chart_key LIKE %s
                GROUP BY chart_key
                ORDER BY chart_key
                """,
                (key_prefix.replace('%', r'\%').replace('_', r'\_') + '%',),
            )
            return cur.fetchall()
    except psycopg2.errors.UndefinedTable:
        conn.rollback()
        return []


# ── Google Ads Campaigns: chosen campaigns ────────────────────────────────────
# Which GA4 campaign IDs the Google Ads Campaigns page shows. db/14_ga4_campaigns.sql
# is the schema; the table is created on first use. Removing a campaign leaves
# its chart_targets rows alone, so its targets return if it is added again.

@once_per_database
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
