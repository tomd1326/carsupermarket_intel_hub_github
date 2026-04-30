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


SETTINGS_DEFAULTS = {
    'number_to_buy':                    200,
    'max_days_sales':                   90,
    'max_dts':                          45,
    'min_gpu_m':                        0,
    'cap_workers':                      25,
    'cap_rate_limit':                   100,
    'calc_buying_reqs':                 '0',
    'enterprise_email_auto_process':    '0',
}

BOOL_SETTINGS = {'calc_buying_reqs', 'enterprise_email_auto_process'}

# calc_buying_reqs is stored per-user (e.g. calc_buying_reqs__buyer).
# enterprise_email_auto_process is a system-level toggle — stored globally, read by Power Automate endpoints.
PER_USER_BOOL_SETTINGS = {'calc_buying_reqs'}
GLOBAL_BOOL_SETTINGS = {'enterprise_email_auto_process'}

ADMIN_USER = 'admin'


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
        # Per-user bool settings: admin always True; others read from user-scoped key.
        for key in PER_USER_BOOL_SETTINGS:
            if user == ADMIN_USER:
                result[key] = True
            else:
                scoped_key = f'{key}__{user}' if user else key
                result[key] = row_map.get(scoped_key, '0') == '1'
        # Global bool settings: read bare key regardless of user.
        for key in GLOBAL_BOOL_SETTINGS:
            result[key] = row_map.get(key, '0') == '1'
        return result
    except Exception:
        d = dict(SETTINGS_DEFAULTS)
        for key in PER_USER_BOOL_SETTINGS:
            d[key] = user == ADMIN_USER
        for key in GLOBAL_BOOL_SETTINGS:
            d[key] = False
        return d


def save_settings(conn, number_to_buy: int, max_days_sales: int, max_dts: int, min_gpu_m: int, cap_workers: int, cap_rate_limit: int, calc_buying_reqs: bool, enterprise_email_auto_process: bool = False, user: str = None):
    values = {
        'number_to_buy':                number_to_buy,
        'max_days_sales':               max_days_sales,
        'max_dts':                      max_dts,
        'min_gpu_m':                    min_gpu_m,
        'cap_workers':                  cap_workers,
        'cap_rate_limit':               cap_rate_limit,
        'enterprise_email_auto_process': '1' if enterprise_email_auto_process else '0',
    }
    # calc_buying_reqs stored per-user; admin's preference is not stored (always True at read time).
    if user != ADMIN_USER:
        suffix = f'__{user}' if user else ''
        values[f'calc_buying_reqs{suffix}'] = '1' if calc_buying_reqs else '0'

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
