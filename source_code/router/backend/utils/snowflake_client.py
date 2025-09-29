# FILE PATH: source_code/router/backend/utils/snowflake_client.py
# LAMBDA-OPTIMIZED: HARDCODED PROD + minimal logging + no emojis

from __future__ import annotations
from typing import Optional, Sequence, Any, Tuple, List

import logging
import boto3
import snowflake.connector
from utils.secrets_manager_client import get_secret

logger = logging.getLogger(__name__)

try:
    import pandas as pd
except Exception:
    pd = None

class SnowflakeClient:
    def __init__(self, environment: str = "prod"):
        # HARDCODED: ALWAYS USE PROD
        environment = "prod"

        # PROD secret and region
        secret_name = "prod/carsupermarket/core/snowflake/data_analyst_credentials"
        region = "eu-west-1"

        secret = get_secret(secret_name, region_name=region)

        self._conn: Optional[snowflake.connector.SnowflakeConnection] = None
        self._cfg = {
            "user": secret["user"],
            "password": secret["password"],
            "account": secret["account"],
            "warehouse": secret["warehouse"],
            "database": secret["database"],
            "schema": secret.get("schema"),
            "role": secret.get("role"),
        }

    def connect(self) -> None:
        if self._conn:
            return
        cfg = {k: v for k, v in self._cfg.items() if v}
        self._conn = snowflake.connector.connect(client_session_keep_alive=True, **cfg)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SnowflakeClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> Tuple[List[tuple], List[str]]:
        if not self._conn:
            raise RuntimeError("Not connected. Use context manager or call connect().")
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params or [])
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            return rows, cols
        finally:
            cur.close()

    def execute_df(self, sql: str, params: Optional[Sequence[Any]] = None):
        if pd is None:
            raise RuntimeError("pandas not installed. Use execute() instead.")
        rows, cols = self.execute(sql, params)
        return pd.DataFrame.from_records(rows, columns=cols)