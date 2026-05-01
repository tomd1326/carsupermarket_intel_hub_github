"""
Adds the uploaded_by column to the tasks table on prod Aurora.

Usage:
    Set AWS_REGION=eu-west-1 in your environment, ensure VPN is connected, then:
    python migrate_03_tasks_uploaded_by.py
"""
import os
import sys

import boto3
import json
import psycopg2


def get_secret(secret_name: str) -> dict:
    region = os.environ['AWS_REGION']
    client = boto3.client('secretsmanager', region_name=region)
    resp = client.get_secret_value(SecretId=secret_name)
    return json.loads(resp['SecretString'])


def get_conn():
    env = 'prod' if os.environ.get('AWS_REGION') == 'eu-west-1' else 'dev'
    varname = f'{env}/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials'
    print(f'Fetching credentials from Secrets Manager: {varname}')
    data = get_secret(varname)
    return psycopg2.connect(
        host=data['endpoint'],
        user=data['username'],
        password=data['password'],
        database='carsupermarket_pricer',
        port=int(data.get('port', 5432)),
    )


MIGRATE_SQL = """
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS uploaded_by VARCHAR(100) NULL;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reductions INTEGER NULL;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS source VARCHAR(20) NULL;
"""


def main():
    if os.environ.get('AWS_REGION') != 'eu-west-1':
        print('ERROR: AWS_REGION must be eu-west-1 to target prod.')
        print('Run: $env:AWS_REGION="eu-west-1"  (PowerShell)')
        sys.exit(1)

    print('Connecting to PROD Aurora (eu-west-1)...')
    conn = get_conn()
    print('Connected.')

    confirm = input('\nThis will ALTER the tasks table on prod. Type YES to continue: ')
    if confirm.strip() != 'YES':
        print('Aborted.')
        conn.close()
        sys.exit(0)

    with conn.cursor() as cur:
        print('\nApplying migration...')
        cur.execute(MIGRATE_SQL)
        conn.commit()
        print('Done. Column uploaded_by added to tasks.')

    conn.close()


if __name__ == '__main__':
    main()
