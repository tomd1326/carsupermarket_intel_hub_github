"""Manual Nexus Point API pulls, written to S3 exactly as the ingestion job does.

Copy of carsupermarket_nexus_point_ingestion (clients/nexus_client.py and
main.py): same retries and waits, same log_id on every row, same gzipped
JSON-lines file name and S3 path. dbt's stg models read these files, so if the
ingestion job changes how it fetches or where it writes, change this to match.
"""
import concurrent.futures
import datetime
import gzip
import json
import logging
import time
from zoneinfo import ZoneInfo

import boto3
import requests

from utils.secrets_manager_client import get_secret

logger = logging.getLogger(__name__)

NEXUS_SECRET_NAME = 'prod/carsupermarket/car_pricer/nexus_point_api_access'
S3_BUCKET = 'aa-raw-sources-uk'
S3_BUCKET_REGION = 'eu-west-1'

# The ingestion job's retry settings: 2 retries after the first try, waiting
# 5s then 10s. Retried on 429/5xx and connection errors; 401/403/404 are not.
MAX_RETRIES = 2
RETRY_DELAYS = [5, 10, 15, 20]
RETRY_STATUSES = {429, 500, 502, 503, 504}


class FetchFailed(Exception):
    pass


class FetchStopped(Exception):
    pass


def normalize_route(route: str) -> str:
    """Single-segment routes end with a slash ('vehicles/'), multi-segment
    ones do not ('invoices/invoice-products') - as the ingestion client."""
    segments = [s for s in (route or '').strip().strip('/').split('/') if s]
    if not segments:
        return ''
    if len(segments) == 1:
        return segments[0] + '/'
    return '/'.join(segments)


def _short(text: str) -> str:
    text = text or ''
    return text[:200] + '...' if len(text) > 200 else text


def fetch_page(host: str, route: str, headers: dict, params: dict, page: int, note=None):
    """One page -> (rows, total_pages). Raises FetchFailed once retries run out."""
    endpoint = f"{host.rstrip('/')}/{normalize_route(route)}"
    req_params = dict(params)
    req_params['page'] = page
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            resp = requests.get(endpoint, headers=headers, params=req_params,
                                timeout=30, allow_redirects=True)
        except requests.RequestException as exc:
            problem = f"connection error: {exc}"
        else:
            status = resp.status_code
            if status == 200:
                # A bad answer fails the endpoint rather than counting as "no
                # rows", so a broken pull is never mistaken for nothing changed.
                if not (resp.text or '').strip():
                    raise FetchFailed(f"empty response on page {page}")
                try:
                    data = resp.json()
                except json.decoder.JSONDecodeError:
                    raise FetchFailed(f"invalid JSON on page {page}: {_short(resp.text)}")
                if not isinstance(data, dict):
                    raise FetchFailed(f"unexpected response on page {page}: {_short(resp.text)}")
                pagination = (data.get('meta') or {}).get('pagination') or {}
                total_pages = (pagination.get('total_pages') or 0) if isinstance(pagination, dict) else 0
                rows = data.get('data')
                if rows is None:
                    rows = []
                if not isinstance(rows, list):
                    raise FetchFailed(f"unexpected 'data' on page {page}: {_short(resp.text)}")
                log_id = data.get('log_id')
                if log_id:
                    for row in rows:
                        if isinstance(row, dict):
                            row['log_id'] = log_id
                return rows, total_pages
            text = _short(resp.text)
            if status == 504 and '<html' in text.lower():
                text = 'Gateway timeout (Cloudflare 504 error)'
            if status not in RETRY_STATUSES:
                raise FetchFailed(f"HTTP {status} on page {page}: {text}")
            problem = f"HTTP {status}: {text}"
        if attempt > MAX_RETRIES:
            raise FetchFailed(f"page {page} failed after {MAX_RETRIES} retries - {problem}")
        delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
        if note:
            note(f"  page {page}: {problem} - retrying in {delay}s ({attempt}/{MAX_RETRIES})")
        time.sleep(delay)
    raise FetchFailed(f"page {page} gave no answer")


def fetch_route(host: str, route: str, per_page: int, workers: int, updated_at: str = None,
                note=None, stop_requested=None) -> list:
    """Every page of one route. Page 1 first to learn the page count, then the
    rest on `workers` threads. Any page failing fails the whole route, so a
    partial file is never uploaded (the ingestion job does the same)."""
    headers = {'Authorization': f"Bearer {get_secret(NEXUS_SECRET_NAME)['access_token']}"}
    params = {'per_page': per_page}
    if updated_at:
        params['updated_at'] = updated_at

    rows, total_pages = fetch_page(host, route, headers, params, 1, note)
    if note:
        note(f"  page 1 of {total_pages or 1} ({len(rows)} rows)")
    if total_pages <= 1:
        return rows

    # Log about 10 progress lines per route, not one per page.
    step = max(1, total_pages // 10)
    done = 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_page, host, route, headers, params, p, note): p
                   for p in range(2, total_pages + 1)}
        try:
            for future in concurrent.futures.as_completed(futures):
                rows.extend(future.result()[0])
                done += 1
                if done % step == 0 or done == total_pages:
                    if note:
                        note(f"  {done} of {total_pages} pages ({len(rows)} rows)")
                    if stop_requested and stop_requested():
                        raise FetchStopped()
        except BaseException:
            for f in futures:
                f.cancel()
            raise
    return rows


def fetch_users_with_units(host: str, per_page: int = 100, workers: int = 4) -> list:
    """Every Nexus user with their site and company name and type, for
    Settings > User Accounts > Import From Nexus. site_id and company_id both
    match id in organisation-units. Users deleted in Nexus are left out.
    Nothing is saved to S3."""
    users = fetch_route(host, 'users', per_page, workers)
    units = {u.get('id'): u for u in fetch_route(host, 'organisation-units', per_page, workers)
             if isinstance(u, dict)}
    out = []
    for u in users:
        if not isinstance(u, dict) or u.get('deleted_at'):
            continue
        site = units.get(u.get('site_id')) or {}
        company = units.get(u.get('company_id')) or {}
        out.append({
            'nexus_user_id': u.get('id'),
            'first_name': u.get('first_name') or '',
            'last_name': u.get('last_name') or '',
            'email': (u.get('email') or '').strip(),
            'site_id': u.get('site_id'),
            'site_name': site.get('name') or '',
            'site_type': site.get('type') or '',
            'company_id': u.get('company_id'),
            'company_name': company.get('name') or '',
            'company_type': company.get('type') or '',
        })
    return out


def upload_route(route: str, rows: list, folder: str) -> str:
    """Gzipped JSON lines to the ingestion job's path. folder is 'v0' (what
    dbt reads) or 'dev'. Stamped with the UK run time, as the job is."""
    now = datetime.datetime.now(ZoneInfo('Europe/London'))
    route = route.strip().strip('/')
    file_name = f"{route.split('/')[-1]}-{now.strftime('%Y%m%d_%H%M%S')}.json.gz"
    date_path = now.strftime('year=%Y/month=%m/day=%d/hour=%H')
    key = f"nexus_point/{route}/json/{folder}/{date_path}/{file_name}"
    body = gzip.compress(''.join(json.dumps(r, default=str) + '\n' for r in rows).encode('utf-8'))
    boto3.client('s3', region_name=S3_BUCKET_REGION).put_object(Bucket=S3_BUCKET, Key=key, Body=body)
    return f"s3://{S3_BUCKET}/{key}"
