import logging
import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from botocore.exceptions import UnauthorizedSSOTokenError
from fastapi import HTTPException
from utils.secrets_manager_client import get_secret

logger = logging.getLogger(__name__)


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


def get_settings(conn) -> dict:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM settings")
            rows = cur.fetchall()
        result = dict(SETTINGS_DEFAULTS)
        for key, value in rows:
            if key in result:
                result[key] = (value == '1') if key in BOOL_SETTINGS else int(value)
        return result
    except Exception:
        d = dict(SETTINGS_DEFAULTS)
        for key in BOOL_SETTINGS:
            d[key] = False
        return d


def save_settings(conn, number_to_buy: int, max_days_sales: int, max_dts: int, min_gpu_m: int, cap_workers: int, cap_rate_limit: int, calc_buying_reqs: bool, enterprise_email_auto_process: bool = False):
    values = {
        'number_to_buy':                    number_to_buy,
        'max_days_sales':                   max_days_sales,
        'max_dts':                          max_dts,
        'min_gpu_m':                        min_gpu_m,
        'cap_workers':                      cap_workers,
        'cap_rate_limit':                   cap_rate_limit,
        'calc_buying_reqs':                 '1' if calc_buying_reqs else '0',
        'enterprise_email_auto_process':    '1' if enterprise_email_auto_process else '0',
    }
    with conn.cursor() as cur:
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
        db_credentials = get_db_credentials()
    except UnauthorizedSSOTokenError:
        logger.error("AWS SSO session has expired. Run: aws sso login")
        raise HTTPException(status_code=503, detail="AWS SSO session has expired. Run: aws sso login")

    connection = psycopg2.connect(
        host=db_credentials['host'],
        user=db_credentials['user'],
        password=db_credentials['password'],
        database=db_credentials['database'],
        port=db_credentials['port']
    )

    try:
        yield connection
    finally:
        connection.close()
