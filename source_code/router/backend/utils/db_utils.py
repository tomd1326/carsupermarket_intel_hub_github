import os
import pymysql
from contextlib import contextmanager
from utils.secrets_manager_client import get_secret


def get_db_credentials():
    if os.getenv('LOCAL_ENV') == '1':
        return {
            'host': os.getenv('DB_HOST', 'localhost'),
            'user': os.getenv('DB_USER', 'user'),
            'password': os.getenv('DB_PASSWORD', 'password'),
            'database': os.getenv('DB_DATABASE', 'test'),
            'port': os.getenv('DB_PORT', 3306)
        }
    elif os.getenv('AWS_REGION') is not None:
        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        varname = f'{env}/common/car_pricer/rds/admin_credentials'
        data = get_secret(varname)
        return {
            'host': data['cluster_endpoint'],
            'user': data['username'],
            'password': data['password'],
            'database': 'test',
            'port': 3306
        }


@contextmanager
def get_database_connection():

    db_credentials = get_db_credentials()

    connection = pymysql.connect(
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
