"""
API response size checker and DB connection audit.

Usage:
    python check_api_sizes.py <VRM> [--env dev|prod] [--mileage 30000] [--skip-db]

Requirements:
    pip install boto3 requests psycopg2-binary

AWS SSO must be active:
    aws sso login
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET

import boto3
import requests


def get_secret(secret_name: str) -> dict:
    # Mirrors the region logic in secrets_manager_client.py
    if secret_name.startswith("prod/"):
        region = "eu-west-1"
    elif secret_name.startswith("dev/"):
        region = "us-east-2"
    else:
        region = "eu-west-1"
    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def _size_label(raw: str) -> str:
    b = len(raw.encode("utf-8"))
    return f"{b:,} bytes ({b / 1024:.1f} KB)"


def check_cap(vrm: str, subscriber_id: str, password: str) -> None:
    import datetime

    print("\n=== CAP API ===")
    today = datetime.date.today().strftime("%Y%m%d")

    # --- VRM lookup ---
    vrm_url = "https://soap.cap.co.uk/vrm/capvrm.asmx/VRMValuation"
    resp = requests.post(
        vrm_url,
        data={
            "SubscriberID": subscriber_id,
            "Password": password,
            "VRM": vrm,
            "Database": "CAR",
            "Date": today,
            "Mileage": 30000,
            "StandardEquipmentRequired": "false",
        },
    )
    raw = resp.text
    print(f"VRM endpoint      HTTP {resp.status_code}  |  {_size_label(raw)}")

    capid = None
    if resp.status_code == 200:
        try:
            root = ET.fromstring(raw)
            ns = "https://soap.cap.co.uk/vrm"
            capid_el = root.find(f"{{{ns}}}CAPIDLookup/{{{ns}}}CAPID")
            capid = capid_el.text if capid_el is not None else None
            print(f"  CAPID:          {capid}")
        except ET.ParseError as e:
            print(f"  XML parse error: {e}")

    # --- Live valuation (mileage bands) ---
    if capid and capid not in ("0", None):
        live_url = "https://soap.cap.co.uk/usedvalueslive/capusedvalueslive.asmx/GetUsedLive_IdRegDate"
        live_resp = requests.post(
            live_url,
            data={
                "SubscriberID": subscriber_id,
                "Password": password,
                "CAPID": capid,
                "TypeCode": "C",
                "RegisteredDate": today,
                "mileage": 30000,
                "StandardEquipmentRequired": "false",
            },
        )
        live_raw = live_resp.text
        print(f"Live valuation    HTTP {live_resp.status_code}  |  {_size_label(live_raw)}")

        if live_resp.status_code == 200:
            try:
                live_root = ET.fromstring(live_raw)
                ns2 = "https://soap.cap.co.uk/usedvalueslive"
                bands = live_root.findall(f".//{{{ns2}}}Valuation")
                print(f"  Mileage bands:  {len(bands)}")
            except ET.ParseError:
                pass

        print(f"\n  Live valuation raw (first 400 chars):\n  {live_raw[:400]}")
    else:
        print("  Skipping live valuation — no CAPID returned")

    print(f"\n  VRM raw (first 400 chars):\n  {raw[:400]}")


def check_autotrader(
    vrm: str, advertiser_id: str, key: str, secret: str, mileage: int = 30000
) -> None:
    print("\n=== AutoTrader API ===")
    host = "https://api.autotrader.co.uk"

    # Authenticate
    auth_resp = requests.post(f"{host}/authenticate", data={"key": key, "secret": secret})
    if auth_resp.status_code != 200:
        print(f"Auth failed: HTTP {auth_resp.status_code} — {auth_resp.text[:200]}")
        return
    token = auth_resp.json().get("access_token")
    headers = {"Authorization": f"Bearer {token}"}

    # /vehicles
    v_resp = requests.get(
        f"{host}/vehicles",
        params={
            "advertiserId": advertiser_id,
            "registration": vrm,
            "valuations": "true",
            "vehicleMetrics": "true",
            "odometerReadingMiles": mileage,
            "includeFirstRegistrationDate": "true",
            "competitors": "false",
            "features": "true",
        },
        headers=headers,
    )
    v_raw = v_resp.text
    print(f"/vehicles         HTTP {v_resp.status_code}  |  {_size_label(v_raw)}")

    derivative_id = None
    first_reg_date = None
    factory_features = []

    if v_resp.status_code == 200:
        v_data = v_resp.json()
        vehicle = v_data.get("vehicle", {})
        derivative_id = vehicle.get("derivativeId")
        first_reg_date = vehicle.get("firstRegistrationDate")
        features = v_data.get("features", [])
        factory_features = [{"name": f["name"]} for f in features if f.get("factoryFitted")]

        print(f"  Top-level keys: {list(v_data.keys())}")
        print(f"  derivativeId:   {derivative_id}")
        print(f"  Factory feats:  {len(factory_features)}")
        print(f"  All feats:      {len(features)}")

    # /valuations
    if derivative_id:
        val_resp = requests.post(
            f"{host}/valuations?advertiserId={advertiser_id}",
            json={
                "vehicle": {
                    "derivativeId": derivative_id,
                    "firstRegistrationDate": first_reg_date,
                    "odometerReadingMiles": mileage,
                },
                "features": factory_features,
            },
            headers=headers,
        )
        val_raw = val_resp.text
        print(f"/valuations       HTTP {val_resp.status_code}  |  {_size_label(val_raw)}")

        if val_resp.status_code == 200:
            print(f"  Top-level keys: {list(val_resp.json().keys())}")

        print(f"\n  /valuations raw:\n  {val_raw[:400]}")
    else:
        print("  Skipping /valuations — no derivativeId from /vehicles")

    print(f"\n  /vehicles raw (first 600 chars):\n  {v_raw[:600]}")


def check_db(env: str) -> None:
    print("\n=== Database Connections ===")
    try:
        import psycopg2

        secret_name = f"{env}/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials"
        data = get_secret(secret_name)

        conn = psycopg2.connect(
            host=data["endpoint"],
            user=data["username"],
            password=data["password"],
            database="carsupermarket_pricer",
            port=int(data.get("port", 5432)),
            connect_timeout=5,
        )

        with conn.cursor() as cur:
            cur.execute("SHOW max_connections;")
            max_conn = cur.fetchone()[0]

            cur.execute("""
                SELECT
                    count(*)                                            AS total,
                    count(*) FILTER (WHERE state = 'active')           AS active,
                    count(*) FILTER (WHERE state = 'idle')             AS idle,
                    count(*) FILTER (WHERE application_name != '')     AS named
                FROM pg_stat_activity
                WHERE datname = 'carsupermarket_pricer';
            """)
            total, active, idle, named = cur.fetchone()

            cur.execute("""
                SELECT application_name, count(*)
                FROM pg_stat_activity
                WHERE datname = 'carsupermarket_pricer'
                GROUP BY application_name
                ORDER BY count(*) DESC;
            """)
            by_app = cur.fetchall()

        conn.close()

        print(f"max_connections:  {max_conn}")
        print(f"Current (carsupermarket_pricer): {total} total / {active} active / {idle} idle")
        print(f"By application:")
        for app, cnt in by_app:
            print(f"  {app or '(unnamed)':40s} {cnt}")

        headroom = int(max_conn) - total
        print(f"\nHeadroom: {headroom} connections remaining")
        print(f"Risk:     Each Lambda instance creates ThreadedConnectionPool(max=10)")
        print(f"          {int(max_conn) // 10} concurrent Lambda invocations would exhaust connections")
        print(f"Fix:      Add RDS Proxy in front of Aurora cluster (Terraform, ~5 lines)")

    except ImportError:
        print("psycopg2 not installed — run: pip install psycopg2-binary")
    except Exception as e:
        print(f"DB check failed: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check API response sizes and DB connections")
    parser.add_argument("vrm", help="Vehicle registration mark (e.g. AB12CDE)")
    parser.add_argument("--env", default="dev", choices=["dev", "prod"])
    parser.add_argument("--mileage", type=int, default=30000)
    parser.add_argument("--skip-db", action="store_true", help="Skip DB connection check")
    args = parser.parse_args()

    vrm = args.vrm.upper().replace(" ", "")
    print(f"VRM: {vrm}  |  env: {args.env}  |  mileage: {args.mileage:,}")

    print(f"\nFetching API credentials from Secrets Manager ({args.env})...")
    try:
        secrets = get_secret(f"{args.env}/carsupermarket/car_pricer/api_access")
    except Exception as e:
        print(f"Failed to fetch secrets: {e}")
        print("Make sure you are logged in: aws sso login")
        sys.exit(1)

    check_cap(
        vrm=vrm,
        subscriber_id=secrets["CAP_SUBSCRIBER_ID"],
        password=secrets["CAP_PASSWORD"],
    )

    check_autotrader(
        vrm=vrm,
        advertiser_id=secrets["ADVERTISER_ID"],
        key=secrets["AUTOTRADER_KEY"],
        secret=secrets["AUTOTRADER_SECRET"],
        mileage=args.mileage,
    )

    if not args.skip_db:
        check_db(args.env)

    print("\nDone.")
