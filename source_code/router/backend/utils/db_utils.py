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


def get_db_credentials():
    if os.getenv('LOCAL_ENV') == '1':
        return {
            'host': os.getenv('DB_HOST', 'localhost'),
            'user': os.getenv('DB_USER', 'test'),
            'password': os.getenv('DB_PASSWORD', 'password'),
            'database': os.getenv('DB_DATABASE', 'test_db'),
            'port': int(os.getenv('DB_PORT', 5432))
        }
    elif os.getenv('AWS_REGION') is not None:
        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        varname = f'{env}/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials'
        data = get_secret(varname)
        return {
            'host': data['endpoint'],
            'user': data['username'],
            'password': data['password'],
            'database': 'carsupermarket_pricer',
            'port': int(data.get('port', 5432))
        }


def get_vehicle_db_credentials():
    """Always returns prod credentials — vehicle/valuation data is always written to prod."""
    if os.getenv('LOCAL_ENV') == '1':
        # In local dev, honour explicit overrides; otherwise fall through to prod secret.
        host = os.getenv('VEHICLE_DB_HOST') or os.getenv('DB_HOST')
        if host:
            return {
                'host': host,
                'user': os.getenv('VEHICLE_DB_USER', os.getenv('DB_USER', 'test')),
                'password': os.getenv('VEHICLE_DB_PASSWORD', os.getenv('DB_PASSWORD', 'password')),
                'database': os.getenv('VEHICLE_DB_DATABASE', os.getenv('DB_DATABASE', 'test_db')),
                'port': int(os.getenv('VEHICLE_DB_PORT', os.getenv('DB_PORT', 5432))),
            }
    varname = 'prod/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials'
    data = get_secret(varname)
    return {
        'host': data['endpoint'],
        'user': data['username'],
        'password': data['password'],
        'database': 'carsupermarket_pricer',
        'port': int(data.get('port', 5432)),
    }


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _db_pool
    if _db_pool is None:
        creds = get_db_credentials()
        _db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=10,
            host=creds['host'], user=creds['user'], password=creds['password'],
            database=creds['database'], port=creds['port'],
        )
    return _db_pool


def _get_vehicle_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _vehicle_db_pool
    if _vehicle_db_pool is None:
        creds = get_vehicle_db_credentials()
        _vehicle_db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=10,
            host=creds['host'], user=creds['user'], password=creds['password'],
            database=creds['database'], port=creds['port'],
        )
    return _vehicle_db_pool


SETTINGS_DEFAULTS = {
    'number_to_buy':                    200,
    'max_days_sales':                   90,
    'max_dts':                          45,
    'min_gpu_m':                        0,
    'cap_workers':                      25,
    'cap_rate_limit':                   100,
    'calc_buying_reqs':                 '0',
    'get_stock':                        '0',
    'evaluator_row_limit':              0,
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


def save_settings(conn, number_to_buy: int, max_days_sales: int, max_dts: int, min_gpu_m: int, cap_workers: int, cap_rate_limit: int, calc_buying_reqs: bool, get_stock: bool = False, evaluator_row_limit: int = 0, user: str = None):
    values = {
        'number_to_buy':                number_to_buy,
        'max_days_sales':               max_days_sales,
        'max_dts':                      max_dts,
        'min_gpu_m':                    min_gpu_m,
        'cap_workers':                  cap_workers,
        'cap_rate_limit':               cap_rate_limit,
        'evaluator_row_limit':          evaluator_row_limit,
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


@contextmanager
def get_database_connection():
    try:
        pool = _get_pool()
    except UnauthorizedSSOTokenError:
        logger.error("AWS SSO session has expired. Run: aws sso login")
        raise HTTPException(status_code=503, detail="AWS SSO session has expired. Run: aws sso login")

    connection = pool.getconn()
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

    connection = pool.getconn()
    try:
        yield connection
    finally:
        pool.putconn(connection)
