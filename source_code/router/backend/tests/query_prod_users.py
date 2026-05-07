"""
Query prod Aurora users table.

Run from source_code/router/backend/:
    python tests/query_prod_users.py
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.db_utils import get_prod_pricer_connection

with get_prod_pricer_connection() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT username, pwd_hash FROM users ORDER BY username")
        rows = cur.fetchall()

print(f"Found {len(rows)} users:")
for username, pwd_hash in rows:
    print(f"  {username}  {pwd_hash[:20]}...")
