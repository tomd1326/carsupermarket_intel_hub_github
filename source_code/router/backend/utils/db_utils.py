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
    'evaluator_row_limit':              0,
    'mot_workers':                      10,
}

BOOL_SETTINGS = {'calc_buying_reqs', 'get_stock'}

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
        for key, value in rows:
            if key in BOOL_SETTINGS or '__' in key:
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
        return result
    except Exception:
        d = dict(SETTINGS_DEFAULTS)
        for key in PER_USER_BOOL_SETTINGS:
            d[key] = user in ALWAYS_ENABLED_USERS
        return d


def save_settings(conn, number_to_buy: int, max_days_sales: int, max_dts: int, min_gpu_m: int, cap_workers: int, cap_rate_limit: int, calc_buying_reqs: bool, get_stock: bool = False, evaluator_row_limit: int = 0, mot_workers: int = 10, at_workers: int = 40, at_rate_limit: int = 60, user: str = None):
    values = {
        'number_to_buy':                number_to_buy,
        'max_days_sales':               max_days_sales,
        'max_dts':                      max_dts,
        'min_gpu_m':                    min_gpu_m,
        'cap_workers':                  cap_workers,
        'cap_rate_limit':               cap_rate_limit,
        'at_workers':                   at_workers,
        'at_rate_limit':                at_rate_limit,
        'evaluator_row_limit':          evaluator_row_limit,
        'mot_workers':                  mot_workers,
    }
    # Per-user bool settings; always-enabled users' preferences are not stored (always True at read time).
    if user not in ALWAYS_ENABLED_USERS:
        suffix = f'__{user}' if user else ''
        values[f'calc_buying_reqs{suffix}'] = '1' if calc_buying_reqs else '0'
        values[f'get_stock{suffix}'] = '1' if get_stock else '0'

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
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (session_id, username, environment, event_type, page,
             json.dumps(metadata) if metadata else None),
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


def remove_user(conn, username: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE username = %s", (username,))
    conn.commit()


@contextmanager
def get_prod_pricer_connection():
    """Always connects to prod pricer DB — used for analytics reads (user_sessions, user_events)."""
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
    """Connection to the prod vehicle DB — always prod regardless of deployment environment."""
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
