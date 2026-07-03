"""S3-backed incremental delta cache for report source data.

Between report runs, the rows fetched from the Nexus Point API (everything
newer than the Snowflake watermark) are cached so the next run only requests
changes since the CACHE's watermark rather than re-fetching the whole
Snowflake-lag window every time. With the report running multiple times an
hour against a 2-hourly dbt refresh, this avoids repeatedly re-downloading
the same (potentially thousands of) changed rows.

Accuracy rules (accuracy always beats speed):
  - The cache is ADVISORY. Missing, unreadable, malformed, or older than the
    Snowflake watermark -> ignored entirely and the run falls back to
    fetching the full window from the Snowflake watermark.
  - Save failures are logged and swallowed — the report is never failed by
    the cache, the next run just refetches.
  - Caches are keyed per API SOURCE (endpoint + filter params) and SHARED
    across reports, so multiple reports using the same endpoint never
    re-request the same data. Cross-report write races are safe because
    every write is self-consistent (loaded cache + own delta) — a lost
    update only causes a refetch, never missing data. Same-report races are
    prevented upstream (single-flight in the start route).

Storage: one gzipped JSON object per source:
  reports/cache/{source_key}.json.gz
  {"watermark": "YYYY-MM-DD HH:MM:SS", "rows": [...]}
Overwritten on every successful run; no history is kept because the cache is
fully re-derivable from the API (and the ingestion pipeline already keeps an
append-only archive of every API payload in this bucket).
"""
import gzip
import json
import logging
import os

import boto3

logger = logging.getLogger(__name__)

_BUCKET = 'aa-raw-sources-uk'
_REGION = 'eu-west-1'  # bucket region, regardless of Lambda region
_ENV = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
_PREFIX = f'applications/pricing/uk-vehicle-list-evaluator/{_ENV}/reports/cache'


def _cache_key(source_key: str) -> str:
    return f'{_PREFIX}/{source_key}.json.gz'


def load_delta_cache(source_key: str):
    """Return {'watermark': str, 'rows': list} or None if absent/unusable."""
    key = _cache_key(source_key)
    try:
        obj = boto3.client('s3', region_name=_REGION).get_object(Bucket=_BUCKET, Key=key)
        data = json.loads(gzip.decompress(obj['Body'].read()))
        if isinstance(data, dict) and data.get('watermark') and isinstance(data.get('rows'), list):
            return data
        logger.warning('Delta cache %s is malformed — ignoring', key)
    except Exception as e:
        logger.info('Delta cache not used for %s: %s', source_key, e)
    return None


def save_delta_cache(source_key: str, watermark: str, rows: list):
    """Best-effort write; never raises (a lost cache just means a refetch)."""
    key = _cache_key(source_key)
    try:
        body = gzip.compress(json.dumps({'watermark': watermark, 'rows': rows}).encode('utf-8'))
        boto3.client('s3', region_name=_REGION).put_object(
            Bucket=_BUCKET, Key=key, Body=body, ContentType='application/json',
        )
        logger.info('Delta cache saved for %s: %d rows, watermark %s',
                    source_key, len(rows), watermark)
    except Exception as e:
        logger.warning('Failed to save delta cache for %s: %s', source_key, e)
