import json
import os
import time
import logging
from typing import Optional

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

_SECRET_CACHE: dict = {}
_SECRET_CACHE_TTL = 300  # seconds


def _infer_region(secret_name: str, default_region: str) -> str:
    if secret_name.startswith("prod/"):
        return "eu-west-1"
    if secret_name.startswith("dev/"):
        return "us-east-2"
    return default_region


def get_secret(secret_name: str, region_name: Optional[str] = None) -> dict:
    """
    Get value from AWS Secrets Manager, cached in memory for 5 minutes.

    Args:
        secret_name: Full secret path, e.g. 'prod/carsupermarket/core/snowflake/data_analyst_credentials'
        region_name: Optional explicit region. If None, inferred from secret_name, then AWS_REGION, then 'eu-west-1'.

    Returns:
        dict: Parsed JSON secret.
    """
    env_region = os.getenv("AWS_REGION", "eu-west-1")
    chosen_region = region_name or _infer_region(secret_name, env_region)
    cache_key = (secret_name, chosen_region)

    cached = _SECRET_CACHE.get(cache_key)
    if cached and time.monotonic() < cached["expires"]:
        return cached["value"]

    logger.debug(f"Getting secret '{secret_name}' from region '{chosen_region}'")

    sm = boto3.client(
        "secretsmanager",
        region_name=chosen_region,
        config=Config(
            connect_timeout=15,
            read_timeout=15,
            retries={"max_attempts": 3},
        ),
    )

    resp = sm.get_secret_value(SecretId=secret_name)
    value = json.loads(resp["SecretString"])
    _SECRET_CACHE[cache_key] = {"value": value, "expires": time.monotonic() + _SECRET_CACHE_TTL}
    return value