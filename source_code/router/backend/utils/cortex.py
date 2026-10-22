"""Snowflake Cortex chat completion, shared by every page's Ask AI."""

import json
import logging
import time

from utils.db_utils import get_database_connection, get_settings
from utils.snowflake_client import SnowflakeClient

logger = logging.getLogger(__name__)

# Model run through Snowflake Cortex (SNOWFLAKE.CORTEX.COMPLETE) rather than a
# third-party API — this account has no Anthropic API key, only a Cortex
# entitlement, so the model has to be one Cortex serves. claude-sonnet-5 is
# confirmed working on this account (2026-08-24) despite eu-west-1 normally
# being outside Anthropic's own regional availability for Claude.
#
# This is only the fallback: the model in use is the one set in Settings > AI.
CORTEX_MODEL = "claude-sonnet-5"


def current_model() -> str:
    """The model chosen in Settings > AI, or the default if it cannot be read."""
    try:
        with get_database_connection() as conn:
            return get_settings(conn).get('cortex_model') or CORTEX_MODEL
    except Exception as e:
        logger.warning("Cortex: could not read the model setting, using %s: %s", CORTEX_MODEL, e)
        return CORTEX_MODEL


def cortex_complete(messages: list[dict], max_tokens: int, temperature: float) -> dict:
    """Run a chat completion through Snowflake Cortex and return the parsed
    response object (the same shape COMPLETE() returns: choices/usage).
    Logs the time taken and tokens used, so slow calls show against the
    API Gateway time limit."""
    model = current_model()
    started = time.monotonic()
    with SnowflakeClient() as client:
        rows, _ = client.execute(
            "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, PARSE_JSON(%s), PARSE_JSON(%s))",
            [model, json.dumps(messages),
             json.dumps({"temperature": temperature, "max_tokens": max_tokens})],
        )
    result = rows[0][0]
    data = json.loads(result) if isinstance(result, str) else result
    usage = data.get("usage", {})
    logger.info(
        "Cortex: model=%s seconds=%.1f tokens_in=%s tokens_out=%s",
        model, time.monotonic() - started,
        usage.get("prompt_tokens"), usage.get("completion_tokens"),
    )
    return data
