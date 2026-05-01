"""
Run this script to reset and recreate all tables on the prod Aurora database.
Drops existing tables in reverse FK order, then recreates from scratch.

Usage:
    Set AWS_REGION=eu-west-1 in your environment, ensure VPN is connected, then:
    python migrate_prod.py
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


DROP_SQL = """
DROP TABLE IF EXISTS supplier_vehicle_status;
DROP TABLE IF EXISTS supplier_vehicle_listings;
DROP TABLE IF EXISTS vehicle_valuations;
DROP TABLE IF EXISTS vehicles;
DROP TABLE IF EXISTS settings;
DROP TABLE IF EXISTS tasks;
DROP TABLE IF EXISTS suppliers;
DROP TYPE IF EXISTS task_status;
"""

CREATE_SQL = """
CREATE TYPE task_status AS ENUM ('created', 'pending', 'success', 'error');

CREATE TABLE suppliers (
    supplier_id   SERIAL PRIMARY KEY,
    supplier_name VARCHAR(250)
);

INSERT INTO suppliers (supplier_name) VALUES
    ('Enterprise'),
    ('Motability'),
    ('1link');

CREATE TABLE tasks (
    task_id          SERIAL PRIMARY KEY,
    created          TIMESTAMP WITHOUT TIME ZONE,
    last_update      TIMESTAMP WITHOUT TIME ZONE,
    status           task_status,
    message          VARCHAR(250),
    supplier_id      INTEGER REFERENCES suppliers(supplier_id),
    filename         VARCHAR(250),
    s3_location      VARCHAR(250),
    file_fingerprint VARCHAR(100),
    total_rows       INTEGER,
    execution_time   FLOAT
);

CREATE TABLE settings (
    key   VARCHAR(100) PRIMARY KEY,
    value VARCHAR(250) NOT NULL
);

INSERT INTO settings (key, value) VALUES
    ('number_to_buy',       '200'),
    ('max_days_sales',      '90'),
    ('max_dts',             '45'),
    ('min_gpu_m',           '0'),
    ('cap_workers',         '25'),
    ('cap_rate_limit',      '100'),
    ('evaluator_row_limit', '0');

CREATE TABLE vehicles (
    vrm                         VARCHAR(10)     PRIMARY KEY,
    at_description              TEXT,
    at_factory_fitted_features  TEXT,
    at_first_registration_date  DATE,
    at_derivative_id            VARCHAR(64),
    cap_capid                   INTEGER,
    cap_capcode                 VARCHAR(50),
    cap_manufacturer            VARCHAR(100),
    cap_model                   VARCHAR(100),
    cap_derivative              TEXT,
    cap_registration_date       VARCHAR(20),
    created_at                  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE TABLE vehicle_valuations (
    id                      BIGSERIAL       PRIMARY KEY,
    vrm                     VARCHAR(10)     NOT NULL REFERENCES vehicles(vrm),
    mileage                 INTEGER         NOT NULL,
    fetched_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    at_retail_valuation     INTEGER,
    at_rating               NUMERIC(6, 2),
    at_days_to_sell         NUMERIC(6, 2),
    at_market_condition     NUMERIC(6, 2),
    cap_live_clean          INTEGER,
    cap_live_retail         INTEGER,
    cap_monthly_clean       INTEGER,
    cap_monthly_retail      INTEGER,
    cap_rounded_mileage     INTEGER
);

CREATE INDEX idx_vehicle_valuations_vrm_mileage_fetched
    ON vehicle_valuations (vrm, mileage, fetched_at DESC);

CREATE TABLE supplier_vehicle_listings (
    id                      BIGSERIAL       PRIMARY KEY,
    vrm                     VARCHAR(10)     NOT NULL REFERENCES vehicles(vrm),
    supplier_id             INTEGER         NOT NULL REFERENCES suppliers(supplier_id),
    imported_at             TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    mileage                 INTEGER,
    asking_price            NUMERIC(10, 2)
);

CREATE INDEX idx_svl_vrm_supplier
    ON supplier_vehicle_listings (vrm, supplier_id, imported_at DESC);

CREATE TABLE supplier_vehicle_status (
    vrm                     VARCHAR(10)     NOT NULL REFERENCES vehicles(vrm),
    supplier_id             INTEGER         NOT NULL REFERENCES suppliers(supplier_id),
    first_seen_at           TIMESTAMPTZ     NOT NULL,
    last_seen_at            TIMESTAMPTZ     NOT NULL,
    current_price           NUMERIC(10, 2),
    previous_price          NUMERIC(10, 2),
    price_changed_at        TIMESTAMPTZ,
    removed_at              TIMESTAMPTZ,
    PRIMARY KEY (vrm, supplier_id)
);

CREATE INDEX idx_svs_active
    ON supplier_vehicle_status (supplier_id)
    WHERE removed_at IS NULL;
"""


def main():
    if os.environ.get('AWS_REGION') != 'eu-west-1':
        print('ERROR: AWS_REGION must be eu-west-1 to target prod.')
        print('Run: $env:AWS_REGION="eu-west-1"  (PowerShell)')
        sys.exit(1)

    print('Connecting to PROD Aurora (eu-west-1)...')
    conn = get_conn()
    print('Connected.')

    confirm = input('\nThis will DROP ALL TABLES on prod and recreate them. Type YES to continue: ')
    if confirm.strip() != 'YES':
        print('Aborted.')
        conn.close()
        sys.exit(0)

    with conn.cursor() as cur:
        print('\nDropping existing tables...')
        cur.execute(DROP_SQL)
        conn.commit()
        print('Dropped.')

        print('Creating tables...')
        cur.execute(CREATE_SQL)
        conn.commit()
        print('Created.')

    conn.close()
    print('\nDone. Prod database is ready.')


if __name__ == '__main__':
    main()
