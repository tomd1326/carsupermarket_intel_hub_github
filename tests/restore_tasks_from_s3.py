import os, sys, json, io
import boto3
import psycopg2
import psycopg2.extras


S3_BUCKET = 'aa-raw-sources-uk'
S3_PREFIX_ELABORATED = 'applications/pricing/uk-vehicle-list-evaluator/prod/elaborated/'


def get_secret(secret_name):
    client = boto3.client('secretsmanager', region_name=os.environ['AWS_REGION'])
    return json.loads(client.get_secret_value(SecretId=secret_name)['SecretString'])


def get_conn():
    data = get_secret('prod/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials')
    return psycopg2.connect(
        host=data['endpoint'], user=data['username'], password=data['password'],
        database='carsupermarket_pricer', port=int(data.get('port', 5432)),
    )


def list_s3_objects(s3, prefix):
    """Returns list of (key, size, last_modified) for all objects under prefix."""
    results = []
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            results.append({
                'key': obj['Key'],
                'size': obj['Size'],
                'last_modified': obj['LastModified'],
            })
    return results


FIRST_COL_SUPPLIER = {
    'Vendor Name': ('1link', 3),
    'Reg No.':     ('Motability', 2),
    'REGNO':       ('Enterprise', 1),
}


def read_file(s3, key):
    """Download file, return (row_count, supplier_name, supplier_id). All may be None on error."""
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        body = obj['Body'].read()
        ext = key.rsplit('.', 1)[-1].lower()
        if ext == 'csv':
            import csv
            text = body.decode('utf-8', errors='replace')
            reader = csv.reader(io.StringIO(text))
            headers = next(reader, [])
            rows = sum(1 for _ in reader)
            first_col = headers[0].strip() if headers else ''
        elif ext in ('xlsx', 'xls'):
            import pandas as pd
            df = pd.read_excel(io.BytesIO(body))
            rows = len(df)
            first_col = df.columns[0] if len(df.columns) else ''
        else:
            return None, None, None

        supplier_name, supplier_id = FIRST_COL_SUPPLIER.get(first_col, (None, None))
        if supplier_name is None:
            print(f'    Warning: unrecognised first column "{first_col}" in {key}')
        return rows, supplier_name, supplier_id
    except Exception as e:
        print(f'    Warning: could not read {key}: {e}')
    return None, None, None


def main():
    if os.environ.get('AWS_REGION') != 'eu-west-1':
        print('ERROR: Set $env:AWS_REGION="eu-west-1" first.')
        sys.exit(1)

    s3 = boto3.client('s3', region_name='eu-west-1')
    conn = get_conn()
    print('Connected to prod.\n')

    print('Listing elaborated files...')
    all_elaborated = list_s3_objects(s3, S3_PREFIX_ELABORATED)
    # Sort by last_modified descending, take the 20 most recent
    all_elaborated.sort(key=lambda o: o['last_modified'], reverse=True)
    elaborated = all_elaborated[:20]
    print(f'Found {len(all_elaborated)} elaborated files, processing the 20 most recent.\n')

    tasks = []
    for obj in elaborated:
        key = obj['key']
        filename_full = key.rsplit('/', 1)[-1]          # e.g. "1234.csv"
        task_id_str, _, ext = filename_full.rpartition('.')
        if not task_id_str.isdigit():
            print(f'  Skipping unexpected key: {key}')
            continue
        task_id = int(task_id_str)

        created = obj['last_modified'].replace(tzinfo=None)
        print(f'  Processing task {task_id} ({ext})...')
        total_rows, supplier_name, supplier_id = read_file(s3, key)

        if supplier_name is None:
            print(f'  Skipping task {task_id} — supplier could not be determined.')
            continue

        filename = f'{supplier_name}_{task_id_str}.{ext}'
        tasks.append({
            'task_id': task_id,
            'created': created,
            'status': 'success',
            'filename': filename,
            'total_rows': total_rows,
            'supplier_id': supplier_id,
        })

    if not tasks:
        print('No tasks found to restore.')
        conn.close()
        return

    print(f'\nRestoring {len(tasks)} tasks...')
    with conn.cursor() as cur:
        # Reset sequence to max task_id so future inserts don't collide
        max_id = max(t['task_id'] for t in tasks)
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO tasks (task_id, created, status, filename, total_rows, supplier_id)
            VALUES %s
            ON CONFLICT (task_id) DO NOTHING
            """,
            [(t['task_id'], t['created'], t['status'], t['filename'], t['total_rows'], t['supplier_id']) for t in tasks],
        )
        cur.execute(f"SELECT setval('tasks_task_id_seq', {max_id})")
        conn.commit()

    print(f'Restored {len(tasks)} task rows.')
    print('Downloads will work as long as the elaborated files are still on S3.')
    conn.close()


if __name__ == '__main__':
    main()
