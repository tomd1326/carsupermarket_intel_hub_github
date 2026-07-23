"""
Remove the old "Generic" supplier row now that "Universal" replaces it.

Any tasks still referencing the Generic supplier_id are repointed to the
Universal supplier_id first (so task history is preserved under the new
name), then the now-unreferenced Generic row is deleted. Requires a
"Universal" row to already exist (see add_suppliers_purchase_pro_universal.py).

Usage:
    python remove_generic_supplier.py [--env dev|prod]

Requirements:
    pip install boto3 psycopg2-binary

AWS SSO must be active:
    aws sso login
"""

import argparse
import json

import boto3
import psycopg2


def get_secret(secret_name: str) -> dict:
    if secret_name.startswith("prod/"):
        region = "eu-west-1"
    else:
        region = "us-east-2"
    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def remove_generic(env: str) -> None:
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
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT supplier_id FROM suppliers WHERE supplier_name = 'Generic'")
            row = cur.fetchone()
            if not row:
                print("No 'Generic' supplier row found — nothing to do.")
                return
            generic_id = row[0]

            cur.execute("SELECT supplier_id FROM suppliers WHERE supplier_name = 'Universal'")
            row = cur.fetchone()
            if not row:
                print("No 'Universal' supplier row found — run add_suppliers_purchase_pro_universal.py first.")
                return
            universal_id = row[0]

            cur.execute("SELECT COUNT(*) FROM tasks WHERE supplier_id = %s", (generic_id,))
            task_count = cur.fetchone()[0]

            if task_count > 0:
                cur.execute(
                    "UPDATE tasks SET supplier_id = %s WHERE supplier_id = %s",
                    (universal_id, generic_id),
                )
                print(f"Repointed {task_count} task(s) from Generic (supplier_id={generic_id}) "
                      f"to Universal (supplier_id={universal_id}).")

            cur.execute("DELETE FROM suppliers WHERE supplier_id = %s", (generic_id,))
            print(f"Deleted 'Generic' supplier (supplier_id={generic_id}).")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Remove the Generic supplier row")
    parser.add_argument("--env", default="dev", choices=["dev", "prod"])
    args = parser.parse_args()

    print(f"env: {args.env}")
    remove_generic(args.env)
    print("Done.")
