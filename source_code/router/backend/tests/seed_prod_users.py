"""
Seed prod Aurora users table with all known users.

Run from source_code/router/backend/:
    python tests/seed_prod_users.py
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.db_utils import get_prod_pricer_connection

USERS = {
    'amartindale':  '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'aneedham':     '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'bcampbell':    '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'cbrown':       '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'dscott':       '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'dwilliams':    '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'ihigh':        '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'jjagger':      '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'jtaylor':      '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'kwalton':      '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'lrickles':     '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'mlewis':       '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'mteal':        '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'ovollans':     '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'rjagger':      '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'srichardson':  '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'tlewis':       '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
}

with get_prod_pricer_connection() as conn:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username VARCHAR(100) PRIMARY KEY,
                pwd_hash VARCHAR(64) NOT NULL
            )
        """)
        for username, pwd_hash in USERS.items():
            cur.execute(
                "INSERT INTO users (username, pwd_hash) VALUES (%s, %s) "
                "ON CONFLICT (username) DO UPDATE SET pwd_hash = EXCLUDED.pwd_hash",
                (username, pwd_hash)
            )
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT username FROM users ORDER BY username")
        rows = cur.fetchall()

print(f"Done. {len(rows)} users in prod:")
for (u,) in rows:
    print(f"  {u}")
