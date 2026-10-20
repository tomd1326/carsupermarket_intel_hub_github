# FILE: utils/snowflake_client.py
#
# DROP THIS FILE INTO: source_code/router/backend/utils/
#
# This is the ONLY new file needed. No changes to any existing file.
#
# PURPOSE:
#   A reusable Snowflake connection wrapper used by both tools:
#     - Supplier Stock Evaluator (this project)
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
#   with SnowflakeClient() as snowflake_client:
#       rows, cols = snowflake_client.execute("SELECT * FROM my_table WHERE CAP_ID = %s", [12345])
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
import threading
import time
import snowflake.connector
import snowflake.connector.errors

from utils import app_log

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


# ----------------------------------------------------------------------
# Connection pool, one per Lambda container
# ----------------------------------------------------------------------
# Opening a Snowflake connection (login) takes 1.4-3.2 s, measured on the Logs
# page 2026-10-20, against 0.1-1 s for most of the app's queries. So a
# `with SnowflakeClient()` block hands its connection back here on exit, and
# the next block in this container takes it instead of logging in again.
#
# Each connection is used by one block at a time (taken out of the list while
# in use), so threads never share one. Idle connections older than
# _MAX_IDLE_SECONDS are closed rather than reused; a reused one that fails with
# a connection error is replaced and the statement retried once (see execute).
#
# Lambda runs one request per container, so the pool only helps requests that
# land on a container that is already warm. With 10+ users that is most of them.
_POOL_MAX_IDLE = 4
_MAX_IDLE_SECONDS = 1800
_pool: list = []          # [(connection, returned_at)]
_pool_lock = threading.Lock()


def _take_idle():
    """An open idle connection, or None."""
    now = time.monotonic()
    stale = []
    conn = None
    with _pool_lock:
        while _pool:
            candidate, returned_at = _pool.pop()
            if now - returned_at > _MAX_IDLE_SECONDS or candidate.is_closed():
                stale.append(candidate)
                continue
            conn = candidate
            break
    for c in stale:
        try:
            c.close()
        except Exception:
            pass
    return conn


def _give_back(conn) -> bool:
    """Put a connection back for reuse. False when the pool is full or the
    connection is already closed, so the caller closes it."""
    try:
        if conn.is_closed():
            return False
    except Exception:
        return False
    with _pool_lock:
        if len(_pool) >= _POOL_MAX_IDLE:
            return False
        _pool.append((conn, time.monotonic()))
        return True


class SnowflakeClient:
    """
    Thin wrapper around the Snowflake Python connector.

    Always connects to PROD. Credentials are fetched from AWS Secrets Manager
    at construction time and never hardcoded.

    Intended to be used as a context manager:

        with SnowflakeClient() as snowflake_client:
            rows, cols = snowflake_client.execute("SELECT 1")

    On exit the connection goes back to this container's pool (see above)
    rather than being closed, even if an exception was raised inside the
    with-block. A connection that failed is closed instead.
    """

    # Path to the Snowflake credentials in AWS Secrets Manager.
    # The secret must be a JSON object containing:
    #   user, password, account, warehouse, database
    # Optional keys: schema, role
    _SECRET_NAME = "prod/carsupermarket/core/snowflake/data_analyst_credentials"

    def __init__(self):
        # Fetch credentials at construction time.
        # Connection is NOT opened yet — that happens in connect() / __enter__().
        started = time.monotonic()
        secret = get_secret(self._SECRET_NAME)
        # Secret fetch + login time, shown on the Logs page against the first
        # query run on this connection, then cleared.
        self._connect_ms: Optional[int] = int((time.monotonic() - started) * 1000)

        self._conn: Optional[snowflake.connector.SnowflakeConnection] = None
        self._reused = False   # came from the pool rather than freshly opened
        self._broken = False   # failed in a way that means it must not be reused

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
    # Context manager — enables `with SnowflakeClient() as snowflake_client:`
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

    def _open_new(self) -> None:
        # Strip None values — Snowflake connector rejects None kwargs
        cfg = {k: v for k, v in self._cfg.items() if v is not None}
        started = time.monotonic()
        self._conn = snowflake.connector.connect(
            client_session_keep_alive=True,  # Prevents Lambda idle timeouts dropping the connection mid-run
            **cfg
        )
        self._reused = False
        self._connect_ms = (self._connect_ms or 0) + int((time.monotonic() - started) * 1000)
        logger.debug("Snowflake connection opened")

    def connect(self) -> None:
        """Take an idle connection from this container's pool, or open a new
        one. Skips silently if already connected."""
        if self._conn:
            return
        conn = _take_idle()
        if conn is not None:
            self._conn = conn
            self._reused = True
            return
        self._open_new()

    def close(self) -> None:
        """Hand the connection back to the pool for the next request, or close
        it if the pool is full or the connection failed."""
        if self._conn:
            conn, self._conn = self._conn, None
            if self._broken or not _give_back(conn):
                try:
                    conn.close()
                except Exception:
                    pass
                logger.debug("Snowflake connection closed")

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None,
                log: bool = True) -> Tuple[List[tuple], List[str]]:
        """
        Execute a SQL query and return raw results.

        Args:
            sql:    SQL string. Use %s as the placeholder for parameters.
            params: Optional list/tuple of values bound to the %s placeholders.
                    Always use this instead of f-strings to avoid SQL injection.
            log:    Record the statement on the Logs page (utils/app_log.py).
                    Only the start of the SQL is stored, never the params.

        Returns:
            rows: list of tuples, one per result row.
            cols: list of column name strings matching the tuple positions.

        Example:
            rows, cols = snowflake_client.execute(
                "SELECT * FROM buying_requirements WHERE CAP_ID = %s",
                [12345]
            )
        """
        if not self._conn:
            raise RuntimeError(
                "Not connected. Use `with SnowflakeClient() as snowflake_client:` "
                "or call snowflake_client.connect() first."
            )
        cur = self._conn.cursor()
        started = time.monotonic()
        rows = None
        error: Optional[BaseException] = None
        try:
            try:
                cur.execute(sql, params or [])
            except (snowflake.connector.errors.OperationalError,
                    snowflake.connector.errors.InterfaceError) as e:
                # A pooled connection whose session ended while idle (network
                # drop, expired login). Open a fresh one and run the statement
                # once more. SQL errors (ProgrammingError) are not retried.
                if not self._reused:
                    self._broken = True
                    raise
                logger.info("Pooled Snowflake connection failed (%s); reconnecting", e)
                try:
                    cur.close()
                except Exception:
                    pass
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
                self._open_new()
                cur = self._conn.cursor()
                started = time.monotonic()
                cur.execute(sql, params or [])
            rows = cur.fetchall()
            # cur.description is None for non-SELECT statements (INSERT, UPDATE etc.)
            cols = [d[0] for d in cur.description] if cur.description else []
            return rows, cols
        except BaseException as e:
            error = e
            if isinstance(e, (snowflake.connector.errors.OperationalError,
                              snowflake.connector.errors.InterfaceError)):
                self._broken = True
            raise
        finally:
            if log:
                try:
                    app_log.record_snowflake(
                        sql, getattr(cur, "sfqid", None),
                        int((time.monotonic() - started) * 1000),
                        len(rows) if rows is not None else None, error,
                        connect_ms=self._connect_ms)
                    self._connect_ms = None
                except Exception:
                    pass  # logging must never change the query's outcome
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
