"""Shared Nexus Point Data API source fetcher with a per-endpoint delta cache.

Any report needing up-to-the-minute rows from a Nexus endpoint calls:

    rows = fetch_updates_since(route, sf_watermark, extra_params, page_progress)

and gets back every row changed since the given Snowflake watermark — served
from the shared cache where possible, topped up from the API for anything
newer. The cache is keyed by endpoint (+ filter params), NOT by report, so
multiple reports using the same endpoint (e.g. vehicles/) share one cache and
the same API data is never requested twice across reports.

Concurrency: safe without locking. Every cache write is self-consistent —
each writer saves exactly (the valid cache it loaded, pruned) + (its own
delta fetched from that cache's watermark), so whichever write survives a
race still covers its full watermark window. A lost update can only cause a
harmless refetch on the next run, never missing data. Same-report races are
already prevented upstream by the single-flight start route.
"""
import concurrent.futures
import datetime
import logging
import re
import time

import requests

from reports.delta_cache import load_delta_cache, save_delta_cache
from utils.secrets_manager_client import get_secret

logger = logging.getLogger(__name__)

NEXUS_API_HOST = 'https://data-api.nexuspointapex.com'
NEXUS_SECRET_NAME = 'prod/carsupermarket/car_pricer/nexus_point_api_access'
NEXUS_PER_PAGE = 100  # API maximum
NEXUS_WORKERS = 4


# ---------------------------------------------------------------------------
# Time helpers (shared by report modules)
# ---------------------------------------------------------------------------

def parse_datetime(value):
    if value is None or value == '':
        return None
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    value = str(value).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def max_updated_at(rows):
    max_dt = None
    for row in rows:
        dt = parse_datetime(row.get('updated_at'))
        if dt is not None and (max_dt is None or dt > max_dt):
            max_dt = dt
    return max_dt.strftime('%Y-%m-%d %H:%M:%S') if max_dt else None


def watermark_with_overlap(watermark, minutes=1):
    """Wind the request watermark back slightly so rows updated in the same
    second as the watermark can't be missed if the API filter is strictly
    greater-than. Refetched rows are harmless (newest state upserts)."""
    dt = parse_datetime(watermark)
    if dt is None:
        return None
    return (dt - datetime.timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------------------
# Raw API fetch
# ---------------------------------------------------------------------------

def _nexus_headers():
    secret = get_secret(NEXUS_SECRET_NAME)
    return {'Authorization': f"Bearer {secret['access_token']}"}


def _fetch_nexus_page(route, headers, params, page):
    """Fetch one page; returns (rows, total_pages). Retries 429/5xx twice."""
    endpoint = f'{NEXUS_API_HOST}/{route}'
    req_params = dict(params)
    req_params['page'] = page
    for attempt in range(3):
        try:
            resp = requests.get(endpoint, headers=headers, params=req_params, timeout=30)
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f'Nexus API request failed for {route} page {page}: {exc}')
        if resp.status_code in (429, 500, 502, 503, 504):
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f'Nexus API error {resp.status_code} for {route} page {page}')
        if resp.status_code != 200:
            raise RuntimeError(f'Nexus API error {resp.status_code} for {route} page {page}: {resp.text[:200]}')
        data = resp.json()
        meta = (data.get('meta') or {}).get('pagination') or {}
        return data.get('data') or [], meta.get('total_pages') or 0
    return [], 0


def fetch_nexus_delta(route, updated_at, extra_params=None, page_progress=None):
    """Fetch all rows changed since `updated_at` from the API.

    page_progress(done_pages, total_pages) is called as pages complete so the
    caller can advance a progress bar within a multi-page fetch.
    """
    headers = _nexus_headers()
    params = {'per_page': NEXUS_PER_PAGE}
    if updated_at:
        params['updated_at'] = updated_at
    if extra_params:
        params.update(extra_params)

    logger.info('Nexus route %s: requesting rows with updated_at >= %s', route, updated_at)
    rows, total_pages = _fetch_nexus_page(route, headers, params, 1)
    if total_pages > 1:
        if page_progress:
            page_progress(1, total_pages)
        with concurrent.futures.ThreadPoolExecutor(max_workers=NEXUS_WORKERS) as ex:
            futures = [ex.submit(_fetch_nexus_page, route, headers, params, p)
                       for p in range(2, total_pages + 1)]
            done = 1
            for future in concurrent.futures.as_completed(futures):
                page_rows, _ = future.result()
                rows.extend(page_rows)
                done += 1
                if page_progress:
                    page_progress(done, total_pages)
    logger.info('Nexus route %s: %d updated rows since %s', route, len(rows), updated_at)
    return rows


# ---------------------------------------------------------------------------
# Cached fetch — the entry point for report modules
# ---------------------------------------------------------------------------

def source_key(route, extra_params=None):
    """Stable cache key for an endpoint + filter combination, e.g.
    'vehicles', 'vehicles-locations', 'contacts_contact-type-supplier'."""
    key = route.strip('/').replace('/', '-')
    if extra_params:
        parts = [f'{k}-{v}' for k, v in sorted(extra_params.items())]
        key += '_' + '_'.join(parts)
    return re.sub(r'[^a-z0-9_-]+', '-', key.lower()).strip('-')


def fetch_updates_since(route, sf_watermark, extra_params=None, page_progress=None):
    """Return every row changed since `sf_watermark`, using the shared
    per-endpoint cache to avoid re-requesting rows previous runs (of any
    report) already fetched. Rows are deduped by id with the live API state
    winning; SOLD/inactive rows are retained (they may be the only record of
    a state change until the warehouse catches up). Also updates the cache.
    """
    key = source_key(route, extra_params)

    # Cache is only trusted when at/after the Snowflake watermark; rows the
    # warehouse has since caught up on are pruned.
    cached_rows = []
    effective_watermark = sf_watermark
    cache = load_delta_cache(key)
    if cache and sf_watermark and cache['watermark'] >= sf_watermark:
        sf_dt = parse_datetime(sf_watermark)
        cached_rows = [r for r in cache['rows']
                       if (parse_datetime(r.get('updated_at')) or datetime.datetime.min) > sf_dt]
        effective_watermark = cache['watermark']
        logger.info('Delta cache for %s: %d rows still ahead of the warehouse, fetching from %s',
                    key, len(cached_rows), effective_watermark)

    delta = fetch_nexus_delta(route, watermark_with_overlap(effective_watermark),
                              extra_params, page_progress)

    # Persist for the next run (any report): everything newer than the
    # warehouse, under the newest watermark we can prove we have.
    next_cache = {}
    for r in cached_rows + delta:
        if r.get('id') is not None:
            next_cache[r['id']] = r
    next_watermark = effective_watermark
    delta_watermark = max_updated_at(delta)
    if delta_watermark and (not next_watermark or delta_watermark > next_watermark):
        next_watermark = delta_watermark
    if next_watermark:
        save_delta_cache(key, next_watermark, list(next_cache.values()))

    # delta last so live API state wins on id conflict when callers merge
    return cached_rows + delta
