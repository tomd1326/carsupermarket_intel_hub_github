# FILE: utils/snowflake_client.py
#
# DROP THIS FILE INTO: source_code/router/backend/utils/
#
# This is the ONLY new file needed. No changes to any existing file.
#
# PURPOSE:
#   A reusable Snowflake connection wrapper used by both tools:
#     - Vehicle List Evaluator (this project)
#     - Buying Requirements (separate project, same table)
#
#   Credentials are pulled from AWS Secrets Manager using the get_secret()
#   utility that already exists in this project — so no new AWS setup is needed.
#
# THE SNOWFLAKE TABLE BOTH TOOLS WILL READ:
#   PROD_CARSUPERMARKET_DB.product_vehicle_journey.buying_requirements
#
# HOW TO USE (in any route or processor file):
#
#   from utils.snowflake_client import SnowflakeClient
#
#   with SnowflakeClient() as sf:
#       rows, cols = sf.execute("SELECT * FROM my_table WHERE CAP_ID = %s", [12345])
#
#   The `with` block handles connecting and disconnecting automatically.
#   Even if an error occurs inside the block, the connection will be closed cleanly.
#
# REQUIRES:
#   pip install snowflake-connector-python
#   (add to requirements.txt alongside existing dependencies)

from __future__ import annotations
from typing import Optional, Sequence, Any, Tuple, List

import logging
import snowflake.connector

# Re-using the existing Secrets Manager utility already in this project.
# NOTE: V1's get_secret() only accepts a single argument (secret_name).
# The V2 version accepted a region_name kwarg — we don't use that here
# because V1's function already reads AWS_REGION from the environment.
from utils.secrets_manager_client import get_secret

logger = logging.getLogger(__name__)

# Optional — only needed if you call execute_df().
# The rest of the client works fine without pandas.
try:
    import pandas as pd
except ImportError:
    pd = None


class SnowflakeClient:
    """
    Thin wrapper around the Snowflake Python connector.

    Always connects to PROD. Credentials are fetched from AWS Secrets Manager
    at construction time and never hardcoded.

    Intended to be used as a context manager:

        with SnowflakeClient() as sf:
            rows, cols = sf.execute("SELECT 1")

    This guarantees the connection is always closed, even if an exception
    is raised inside the with-block.
    """

    # Path to the Snowflake credentials in AWS Secrets Manager.
    # The secret must be a JSON object containing:
    #   user, password, account, warehouse, database
    # Optional keys: schema, role
    _SECRET_NAME = "prod/carsupermarket/core/snowflake/data_analyst_credentials"

    def __init__(self):
        # Fetch credentials at construction time.
        # Connection is NOT opened yet — that happens in connect() / __enter__().
        secret = get_secret(self._SECRET_NAME)

        self._conn: Optional[snowflake.connector.SnowflakeConnection] = None

        # Build config dict from the secret.
        # schema and role are optional — .get() returns None if absent,
        # and None values are stripped out before connecting (see connect()).
        self._cfg = {
            "user":      secret["user"],
            "password":  secret["password"],
            "account":   secret["account"],   # e.g. "xy12345.eu-west-1"
            "warehouse": secret["warehouse"],
            "database":  secret["database"],
            "schema":    secret.get("schema"),
            "role":      secret.get("role"),
        }

    # ------------------------------------------------------------------
    # Context manager — enables `with SnowflakeClient() as sf:`
    # ------------------------------------------------------------------

    def __enter__(self) -> "SnowflakeClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Always close on exit, whether or not an exception occurred.
        self.close()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the connection. Skips silently if already connected."""
        if self._conn:
            return
        # Strip None values — Snowflake connector rejects None kwargs
        cfg = {k: v for k, v in self._cfg.items() if v is not None}
        self._conn = snowflake.connector.connect(
            client_session_keep_alive=True,  # Prevents Lambda idle timeouts dropping the connection mid-run
            **cfg
        )
        logger.debug("Snowflake connection opened")

    def close(self) -> None:
        """Close the connection if open."""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.debug("Snowflake connection closed")

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> Tuple[List[tuple], List[str]]:
        """
        Execute a SQL query and return raw results.

        Args:
            sql:    SQL string. Use %s as the placeholder for parameters.
            params: Optional list/tuple of values bound to the %s placeholders.
                    Always use this instead of f-strings to avoid SQL injection.

        Returns:
            rows: list of tuples, one per result row.
            cols: list of column name strings matching the tuple positions.

        Example:
            rows, cols = sf.execute(
                "SELECT * FROM buying_requirements WHERE CAP_ID = %s",
                [12345]
            )
        """
        if not self._conn:
            raise RuntimeError(
                "Not connected. Use `with SnowflakeClient() as sf:` "
                "or call sf.connect() first."
            )
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params or [])
            rows = cur.fetchall()
            # cur.description is None for non-SELECT statements (INSERT, UPDATE etc.)
            cols = [d[0] for d in cur.description] if cur.description else []
            return rows, cols
        finally:
            cur.close()  # Always release the cursor

    def execute_df(self, sql: str, params: Optional[Sequence[Any]] = None):
        """
        Execute a SQL query and return the result as a pandas DataFrame.

        Convenience wrapper around execute() for when you want to work with
        the data as a DataFrame rather than raw tuples.

        Raises RuntimeError if pandas is not installed.
        """
        if pd is None:
            raise RuntimeError(
                "pandas is not installed. "
                "Use execute() to get raw rows, or `pip install pandas`."
            )
        rows, cols = self.execute(sql, params)
        return pd.DataFrame.from_records(rows, columns=cols)
