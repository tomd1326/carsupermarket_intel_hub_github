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
