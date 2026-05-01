"""
Quick check of prod database table row counts.
Usage:
    $env:AWS_REGION="eu-west-1"
    python ..\..\..\db\check_prod.py
"""
import os, sys, json
import boto3
import psycopg2

def get_secret(secret_name):
    client = boto3.client('secretsmanager', region_name=os.environ['AWS_REGION'])
    return json.loads(client.get_secret_value(SecretId=secret_name)['SecretString'])

def get_conn():
    env = 'prod' if os.environ.get('AWS_REGION') == 'eu-west-1' else 'dev'
    data = get_secret(f'{env}/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials')
    return psycopg2.connect(
        host=data['endpoint'], user=data['username'], password=data['password'],
        database='carsupermarket_pricer', port=int(data.get('port', 5432)),
    )

TABLES = [
    'suppliers', 'tasks', 'settings', 'vehicles',
    'vehicle_valuations', 'supplier_vehicle_listings', 'supplier_vehicle_status',
]

def main():
    env = os.environ.get('AWS_REGION', 'not set')
    print(f'AWS_REGION: {env}')
    print('Connecting...')
    conn = get_conn()
    print('Connected.\n')
    with conn.cursor() as cur:
        for table in TABLES:
            try:
                cur.execute(f'SELECT COUNT(*) FROM {table}')
                count = cur.fetchone()[0]
                print(f'  {table}: {count} rows')
            except Exception as e:
                print(f'  {table}: ERROR — {e}')
                conn.rollback()
    conn.close()

if __name__ == '__main__':
    main()
