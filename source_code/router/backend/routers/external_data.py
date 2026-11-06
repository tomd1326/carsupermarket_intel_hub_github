"""Settings > External Data: SMMT and cap hpi publications, admin only.

Raw files (zips, spreadsheets, news releases, report PDFs) are kept unchanged in S3 under
utils.smmt.EXTERNAL_ROOT/<publisher>/<feed>/<period>/, with their source link saved as S3
object metadata, so S3 is the full record and the tables here can always be rebuilt from
it. What each file holds is read once (utils/smmt.py, utils/cap_hpi.py) and kept with the
file in `ext_files.parsed`; the data tables are then rebuilt from those in one go.

The page drives a check in small steps (scan one listing page, fetch one file) so no single
request runs near the API Gateway time limit, then calls /notify so new files send the email
alerts. auto_run does the same steps in one go for an hourly timer, but nothing calls it yet:
EventBridge and Rundeck are not available (Tom, 2026-11-06), so checks are started from the
page. Only one check runs at a time: each takes the 'check' row in ext_jobs first.

Tables are in the prod pricer cluster, like kpi_layout, so local and prod show the same data.
The home page is not changed yet: home_news_enabled / home_news_admin_only are kept in the
config for the future news panel.
"""
import concurrent.futures
import datetime
import hashlib
import logging
import re
import urllib.parse
import uuid

import boto3
import psycopg2.extras
import requests
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

from utils import cap_hpi, external_checks, smmt
from utils.db_utils import get_prod_pricer_connection
from utils.ddl_once import once_per_database

from .core import get_current_user, is_admin, s3_bucketname, s3_bucket_region, _uk_now
from . import kpi

logger = logging.getLogger(__name__)

router = APIRouter()

_KEY = 'external_data'
_STATE_KEY = 'external_data_state'   # written by the checks; kept apart so a settings save never overwrites it
_API = '/api/external-data'
_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120 Safari/537.36'}
_LISTING = ('https://www.smmt.co.uk/news/?_sft_category=news&_sft_post_category=car'
            '&_sft_subcategory=car-registrations&post_date=+31122026')
_CAP_LISTING = 'https://www.cap-hpi.com/tag/cars/'
_LEASE_MINUTES = 20
_REFRESH_DAYS = 60

DEFAULT_SOURCES = [
    {'name': 'Car Registrations Page', 'url': 'https://www.smmt.co.uk/vehicle-data/car-registrations/',
     'feed': 'new_car_registrations', 'type': 'page', 'auto': 'off', 'auto_days': 5},
    {'name': 'Car Registrations News', 'url': _LISTING, 'feed': 'new_car_registrations', 'type': 'listing',
     'auto': 'off', 'auto_days': 5},
    {'name': 'Used Car Sales Page', 'url': 'https://www.smmt.co.uk/vehicle-data/used-car-sales/',
     'feed': 'used_car_sales', 'type': 'page', 'auto': 'off', 'auto_days': 5},
    {'name': 'New Car And Van Outlook Page', 'url': 'https://www.smmt.co.uk/vehicle-data/uk-new-car-and-van-outlook/',
     'feed': 'outlook', 'type': 'page', 'auto': 'off', 'auto_days': 5},
    {'name': 'CAP HPI Car Editorials', 'url': _CAP_LISTING, 'feed': 'cap_market_overview', 'type': 'listing',
     'auto': 'month_end', 'auto_days': 3},
]

DEFAULT_CONFIG = {
    'sources': DEFAULT_SOURCES,
    'seeded': [s['url'] for s in DEFAULT_SOURCES],
    'skip_words': smmt.DEFAULT_SKIP_WORDS,
    'auto_check_hours': 6,
    'auto_hours': {'from': 8, 'to': 18},
    'home_news_enabled': False,
    'home_news_admin_only': True,
    # Month grid colours, one per way a figure was obtained, plus missing, and the cap hpi up / down cells.
    'colours': {'last_year': '#2e8b57', 'own_file': '#7ed321', 'tracker': '#4a90d9',
                'worked_out': '#f5a623', 'missing': '#d0021b', 'cap_up': '#2e8b57', 'cap_down': '#c0392b'},
}
_FEED_LABELS = {'new_car_registrations': 'SMMT New Car Registrations', 'used_car_sales': 'SMMT Used Car Sales',
                'outlook': 'SMMT Outlook', 'cap_market_overview': 'CAP HPI Car Market Overview',
                'cap_future_overview': 'CAP HPI Future Car Market Overview'}
_AUTO_LABELS = {'off': 'Off', 'daily': 'Daily', 'month_start': 'Start Of Month', 'month_end': 'End Of Month'}


def _admin(user):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")


def _clean_source(s: dict) -> dict:
    auto = s.get('auto') if s.get('auto') in external_checks.MODES else 'off'
    try:
        days = max(1, min(15, int(s.get('auto_days') or 3)))
    except (TypeError, ValueError):
        days = 3
    return {'name': str(s.get('name') or s.get('url') or '')[:80], 'url': str(s.get('url') or '').strip(),
            'feed': s.get('feed'), 'type': s.get('type'), 'auto': auto, 'auto_days': days}


def _config() -> dict:
    stored = kpi._read_kpi_layout_value(_KEY)
    stored = stored if isinstance(stored, dict) else {}
    cfg = {**DEFAULT_CONFIG, **stored}
    cfg['colours'] = {**DEFAULT_CONFIG['colours'], **(stored.get('colours') or {})}
    cfg['auto_hours'] = {**DEFAULT_CONFIG['auto_hours'], **(stored.get('auto_hours') or {})}
    # A default source added after the config was first saved (cap hpi, 2026-11-06) is added
    # once; one Tom removed later stays removed.
    seeded = list(stored.get('seeded') or ([s['url'] for s in DEFAULT_SOURCES[:4]] if stored.get('sources') else []))
    sources = [_clean_source(s) for s in (stored.get('sources') or [])]
    for s in DEFAULT_SOURCES:
        if s['url'] not in seeded and all(x['url'] != s['url'] for x in sources):
            sources.append(dict(s))
    cfg['sources'] = sources if stored.get('sources') else [dict(s) for s in DEFAULT_SOURCES]
    cfg['seeded'] = sorted(set(seeded) | {s['url'] for s in DEFAULT_SOURCES})
    return cfg


def _save_config(values: dict) -> dict:
    cfg = {**_config(), **values}
    kpi._write_kpi_layout_value(_KEY, cfg)
    return cfg


def _state() -> dict:
    stored = kpi._read_kpi_layout_value(_STATE_KEY)
    return stored if isinstance(stored, dict) else {}


def _save_state(values: dict) -> dict:
    state = {**_state(), **values}
    kpi._write_kpi_layout_value(_STATE_KEY, state)
    return state


def _s3():
    return boto3.client('s3', region_name=s3_bucket_region)


def _iso(v):
    return v.isoformat() if isinstance(v, (datetime.date, datetime.datetime)) else v


def _conn():
    return get_prod_pricer_connection()


# ---------------------------------------------------------------- tables

@once_per_database
def _ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS ext_files ("
            " id SERIAL PRIMARY KEY,"
            " s3_key TEXT NOT NULL UNIQUE,"
            " feed VARCHAR(40) NOT NULL,"
            " period VARCHAR(20) NOT NULL,"
            " filename TEXT NOT NULL,"
            " kind VARCHAR(20),"
            " source_url TEXT,"
            " post_url TEXT,"
            " published DATE,"
            " sha256 CHAR(64) NOT NULL,"
            " size_bytes INTEGER,"
            " fetched_at TIMESTAMP NOT NULL DEFAULT NOW(),"
            " page_note TEXT,"
            " final_date DATE,"
            " status VARCHAR(12) NOT NULL DEFAULT 'final',"
            " superseded BOOLEAN NOT NULL DEFAULT FALSE,"
            " headline TEXT,"
            " parsed JSONB,"
            " rows_loaded INTEGER NOT NULL DEFAULT 0,"
            " error TEXT)"
        )
        # Added 2026-11-06: report titles (cap hpi), and the HTTP validators used to spot a
        # file the publisher replaced at the same link.
        cur.execute("ALTER TABLE ext_files ADD COLUMN IF NOT EXISTS title TEXT")
        cur.execute("ALTER TABLE ext_files ADD COLUMN IF NOT EXISTS etag TEXT")
        cur.execute("ALTER TABLE ext_files ADD COLUMN IF NOT EXISTS last_modified TEXT")
        cur.execute("ALTER TABLE ext_files ADD COLUMN IF NOT EXISTS checked_at TIMESTAMP")
        cur.execute("CREATE INDEX IF NOT EXISTS ext_files_source_url ON ext_files (source_url)")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS smmt_car_totals ("
            " month CHAR(7) PRIMARY KEY, units INTEGER NOT NULL, how VARCHAR(12) NOT NULL,"
            " file_id INTEGER, private_units INTEGER, fleet_units INTEGER)"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS smmt_car_marques ("
            " month CHAR(7) NOT NULL, marque VARCHAR(60) NOT NULL, units INTEGER NOT NULL,"
            " how VARCHAR(12) NOT NULL, file_id INTEGER, PRIMARY KEY (month, marque))"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS smmt_used_car_sales ("
            " month CHAR(7) PRIMARY KEY, units INTEGER NOT NULL, file_id INTEGER)"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS smmt_outlook ("
            " file_id INTEGER NOT NULL, published DATE, year INTEGER NOT NULL, forecast BOOLEAN NOT NULL,"
            " measure VARCHAR(60) NOT NULL, thousands NUMERIC NOT NULL, PRIMARY KEY (file_id, year, measure))"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS cap_value_movements ("
            " month CHAR(7) NOT NULL, tbl VARCHAR(12) NOT NULL, segment VARCHAR(60) NOT NULL,"
            " age_band VARCHAR(12) NOT NULL, pct NUMERIC, partial BOOLEAN NOT NULL DEFAULT FALSE,"
            " file_id INTEGER, seq INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (month, tbl, segment, age_band))"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS cap_notable_movers ("
            " month CHAR(7) NOT NULL, band VARCHAR(10) NOT NULL, model VARCHAR(120) NOT NULL,"
            " min_gbp NUMERIC, max_gbp NUMERIC, avg_gbp NUMERIC, file_id INTEGER, seq INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (month, band, model))"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS cap_monthly ("
            " month CHAR(7) PRIMARY KEY, pct_3yr_60k NUMERIC, file_id INTEGER)"
        )
        # One row: who is running a check, until when. See _take_lease.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS ext_jobs ("
            " name VARCHAR(20) PRIMARY KEY, holder VARCHAR(60) NOT NULL, expires_at TIMESTAMP NOT NULL)"
        )
    conn.commit()


# ---------------------------------------------------------------- one check at a time

def _take_lease(conn, holder: str) -> bool:
    """Claim the check for `holder` for _LEASE_MINUTES, or renew it if it already has it.
    False when another check holds it and has not run out. A second browser tab, or a
    timer firing twice, therefore cannot run alongside."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ext_jobs (name, holder, expires_at) VALUES ('check', %s, NOW() + %s * INTERVAL '1 minute')"
            " ON CONFLICT (name) DO UPDATE SET holder = EXCLUDED.holder, expires_at = EXCLUDED.expires_at"
            " WHERE ext_jobs.expires_at < NOW() OR ext_jobs.holder = EXCLUDED.holder RETURNING holder",
            (holder, _LEASE_MINUTES))
        ok = cur.fetchone() is not None
    conn.commit()
    return ok


def _release_lease(conn, holder: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ext_jobs WHERE name = 'check' AND holder = %s", (holder,))
    conn.commit()


def _lease_holder(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT holder, expires_at FROM ext_jobs WHERE name = 'check' AND expires_at >= NOW()")
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- reading publisher pages

def _get(url: str, timeout: int = 25) -> requests.Response:
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp


_OLD_UPLOADS = 'https://www.smmt.co.uk/wp-content/uploads/'


def _download(url: str) -> tuple:
    """(response or None, bytes, archive link or None). Posts before late 2022 link
    www.smmt.co.uk/wp-content/uploads/<file>, which SMMT no longer serves; the Wayback
    Machine kept them under uploads/sites/2/."""
    try:
        resp = _get(url)
        return resp, resp.content, None
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 404 or not url.startswith(_OLD_UPLOADS):
            raise
    for candidate in (url.replace(_OLD_UPLOADS, _OLD_UPLOADS + 'sites/2/'), url):
        found = requests.get('https://archive.org/wayback/available', params={'url': candidate},
                             headers=_HEADERS, timeout=20).json()
        snap = (found.get('archived_snapshots') or {}).get('closest') or {}
        if snap.get('available') and snap.get('timestamp'):
            # id_ asks for the file exactly as saved, without the archive's page around it.
            archive = f"https://web.archive.org/web/{snap['timestamp']}id_/{candidate}"
            return None, _get(archive, timeout=25).content, archive
    raise ValueError(f'{url}: gone from SMMT and not in the Wayback Machine')


def _smmt_post(post_url: str) -> dict:
    """One SMMT news post: its published date and the files it links."""
    html = _get(post_url).text
    m = (re.search(r'article:published_time" content="(20\d\d-\d\d-\d\d)', html)
         or re.search(r'datetime="(20\d\d-\d\d-\d\d)', html))
    return {'post_url': post_url, 'published': m.group(1) if m else None, 'title': None,
            'links': smmt.page_links(html, post_url)}


_CAP_WRITTEN = re.compile(r'correct at the time of writing,?\s*(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(20\d\d)', re.I)


def _cap_post(post_url: str) -> dict:
    """One cap hpi editorial: its title, the date it was written and its report PDF."""
    html = _get(post_url).text
    t = re.search(r'<title>([^<|]+)', html)
    title = re.sub(r'\s+', ' ', t.group(1)).strip() if t else post_url.rstrip('/').rsplit('/', 1)[-1]
    written = None
    m = _CAP_WRITTEN.search(re.sub(r'<[^>]+>', ' ', html))
    if m:
        try:
            written = datetime.datetime.strptime(f"{m.group(1)} {m.group(2)[:3]} {m.group(3)}", '%d %b %Y').date().isoformat()
        except ValueError:
            written = None
    links = [u for u in smmt.page_links(html, post_url, ('pdf',))
             if '/wp-content/uploads/20' in u and 'gender' not in u.lower()]
    return {'post_url': post_url, 'published': written, 'title': title, 'links': links}


def _safe(fn, url: str) -> dict:
    try:
        return fn(url)
    except Exception as e:
        logger.warning("External data post %s not read: %s", url, e)
        return {'post_url': url, 'published': None, 'title': None, 'links': []}


def _is_cap(url: str) -> bool:
    return 'cap-hpi.com' in (url or '')


def _listing_page_url(url: str, page: int) -> str:
    if page <= 1:
        return url
    if _is_cap(url):
        return url.rstrip('/') + f'/page/{page}/'
    return f"{url}&sf_paged={page}"


def _listing_posts(url: str, html: str) -> list:
    """Post links on a listing page, once each."""
    if _is_cap(url):
        found = [u for u in re.findall(r'href="(https://www\.cap-hpi\.com/editorials/[a-z0-9-]+/)"', html)
                 if cap_hpi.series(u.rstrip('/').rsplit('/', 1)[-1])]
    else:
        # SMMT result tiles carry single-news-card; menu links do not.
        found = re.findall(r'<a[^>]+href="(https://www\.smmt\.co\.uk/[a-z0-9-]{20,}/)"[^>]*single-news-card', html)
        if not found:
            found = re.findall(r'href="(https://www\.smmt\.co\.uk/[a-z0-9-]{25,}/)"', html)
    return list(dict.fromkeys(found))


def _next_page(url: str, html: str, page: int):
    if _is_cap(url):
        return page + 1 if f'/page/{page + 1}/' in html else None
    pages = [int(n) for n in re.findall(r'sf_paged=(\d+)', html)]
    return page + 1 if pages and page < max(pages) else None


def _known_urls(conn) -> set:
    with conn.cursor() as cur:
        cur.execute("SELECT source_url FROM ext_files WHERE source_url IS NOT NULL")
        return {r[0] for r in cur.fetchall()}


def _scan_source(cfg: dict, source: dict, page: int, known: set) -> dict:
    """New file links on one source page: {'links': [...], 'next_page': n | None}."""
    found, next_page = [], None
    if source['type'] == 'page':
        html = _get(source['url']).text
        note = smmt.page_note(html)
        for url in smmt.page_links(html, source['url']):
            found.append({'url': url, 'page_note': note, 'post_url': source['url'], 'published': None, 'title': None})
    else:
        html = _get(_listing_page_url(source['url'], page)).text
        next_page = _next_page(source['url'], html, page)
        reader = _cap_post if _is_cap(source['url']) else _smmt_post
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            for res in ex.map(lambda u: _safe(reader, u), _listing_posts(source['url'], html)):
                for url in res['links']:
                    found.append({'url': url, 'page_note': None, 'post_url': res['post_url'],
                                  'published': res['published'], 'title': res['title']})
    links, seen = [], set()
    for f in found:
        filename = urllib.parse.unquote(f['url'].rsplit('/', 1)[-1])
        if _is_cap(f['url']):
            s = cap_hpi.series(f['title'] or filename)
            feed = s[0] if s else None
        else:
            feed = smmt.feed_for(filename, source['feed'], cfg.get('skip_words'))
        if feed is None or f['url'] in known or f['url'] in seen:
            continue
        seen.add(f['url'])
        links.append({**f, 'feed': feed, 'filename': filename})
    return {'links': links, 'next_page': next_page}


def _refresh_links(conn) -> list:
    """Recent files that may have been replaced at the same link. A HEAD request is compared
    with the etag, Last-Modified and size saved with each file (external_checks.refresh_decision);
    where that cannot tell, the file is downloaded again and its sha256 compared by _store."""
    since = _uk_now() - datetime.timedelta(days=_REFRESH_DAYS)
    with conn.cursor() as cur:
        cur.execute("SELECT id, source_url, feed, post_url, published, title, etag, last_modified, size_bytes"
                    " FROM ext_files WHERE NOT superseded AND source_url IS NOT NULL"
                    " AND COALESCE(published::timestamp, fetched_at) >= %s", (since,))
        rows = cur.fetchall()

    def head(row):
        try:
            r = requests.head(row[1], headers=_HEADERS, timeout=15, allow_redirects=True)
            return row, r.status_code, dict(r.headers)
        except Exception:
            return row, None, None
    out, record = [], []
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        for row, status, headers in ex.map(head, rows):
            decision = external_checks.refresh_decision(
                {'etag': row[6], 'last_modified': row[7], 'size': row[8]}, status, headers)
            if decision == 'download':
                out.append({'url': row[1], 'feed': row[2], 'post_url': row[3], 'published': _iso(row[4]),
                            'title': row[5], 'filename': row[1].rsplit('/', 1)[-1], 'refresh': True})
            elif decision == 'record':
                h = {k.lower(): v for k, v in headers.items()}
                record.append((h.get('etag'), h.get('last-modified'), row[0]))
    with conn.cursor() as cur:
        if record:
            psycopg2.extras.execute_batch(cur, "UPDATE ext_files SET etag = %s, last_modified = %s,"
                                               " checked_at = NOW() WHERE id = %s", record)
        cur.execute("UPDATE ext_files SET checked_at = NOW() WHERE id = ANY(%s)", ([r[0] for r in rows],))
    conn.commit()
    return out


# ---------------------------------------------------------------- storing one file

def _read_file(feed: str, filename: str, data: bytes, title: str | None, published: str | None):
    """(info, parsed for ext_files.parsed, rows, period) for one raw file."""
    if feed in cap_hpi.FEEDS:
        info = cap_hpi.classify(feed, filename, data, title or filename)
        parsed = {k: info.get(k) for k in ('movements', 'movers', 'overall_3yr_60k', 'month', 'report_type')}
        rows = len(info['movements']) + len(info['movers'])
        period = info.get('month') or 'undated'
    else:
        info = smmt.classify(feed, filename, data)
        parsed = {k: info.get(k) for k in ('marques', 'tracker', 'used', 'outlook', 'month', 'published')}
        rows = (len((info.get('marques') or {}).get('rows', [])) + len(info.get('tracker') or [])
                + len(info.get('used') or []) + len(info.get('outlook') or []))
        period = smmt.period_for(feed, info, filename, published)
    return info, parsed, rows, period


def _store(conn, feed: str, filename: str, data: bytes, meta: dict) -> dict:
    """Save a raw file to S3 (unless the same bytes are already there) and record what it holds.
    Returns {'id', 'outcome': 'saved' | 'unchanged', ...}."""
    sha = hashlib.sha256(data).hexdigest()
    published, title = meta.get('published'), meta.get('title')
    try:
        info, parsed, rows, period = _read_file(feed, filename, data, title, published)
        error = None
    except Exception as e:
        logger.warning("External data file %s not read: %s", filename, e)
        info, parsed, rows, error = {'kind': 'other'}, None, 0, str(e)[:500]
        period = (cap_hpi.report_month(title or filename) if feed in cap_hpi.FEEDS else None) or 'undated'
    key = smmt.s3_key(feed, period, filename)
    with conn.cursor() as cur:
        # The file at this link, or at this key, saved before.
        cur.execute("SELECT id, sha256, s3_key FROM ext_files WHERE NOT superseded AND (s3_key = %s"
                    " OR (source_url IS NOT NULL AND source_url = %s)) ORDER BY fetched_at DESC LIMIT 1",
                    (key, meta.get('url')))
        row = cur.fetchone()
        if row and row[1] == sha:
            cur.execute("UPDATE ext_files SET etag = COALESCE(%s, etag), last_modified = COALESCE(%s, last_modified),"
                        " checked_at = NOW() WHERE id = %s", (meta.get('etag'), meta.get('last_modified'), row[0]))
            conn.commit()
            return {'id': row[0], 'outcome': 'unchanged', 's3_key': row[2]}
        if row:
            # Same link or name, different bytes: the publisher replaced it. Keep both; newest wins.
            if row[2] == key:
                stem, dot, ext = filename.rpartition('.')
                key = smmt.s3_key(feed, period, f"{stem or ext}__{_uk_now():%Y%m%d-%H%M%S}{dot}{ext if stem else ''}")
            cur.execute("UPDATE ext_files SET superseded = TRUE WHERE id = %s", (row[0],))
    s3_meta = {k: urllib.parse.quote(str(v))[:1000] for k, v in {
        'source_url': meta.get('url'), 'post_url': meta.get('post_url'), 'archive_url': meta.get('archive_url'),
        'published': published, 'title': title, 'page_note': meta.get('page_note'),
        'fetched_at': _uk_now().isoformat(timespec='seconds')}.items() if v}
    _s3().put_object(Bucket=s3_bucketname, Key=key, Body=data, Metadata=s3_meta)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ext_files (s3_key, feed, period, filename, kind, source_url, post_url, published, sha256,"
            " size_bytes, fetched_at, page_note, headline, parsed, rows_loaded, error, title, etag, last_modified,"
            " checked_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())"
            " ON CONFLICT (s3_key) DO UPDATE SET sha256 = EXCLUDED.sha256, kind = EXCLUDED.kind,"
            " parsed = EXCLUDED.parsed, rows_loaded = EXCLUDED.rows_loaded, error = EXCLUDED.error,"
            " headline = EXCLUDED.headline, superseded = FALSE"
            " RETURNING id",
            (key, feed, period, filename, info.get('kind'), meta.get('url'), meta.get('post_url'),
             published or None, sha, len(data), meta.get('fetched_at') or _uk_now(), meta.get('page_note'),
             info.get('headline') or None, psycopg2.extras.Json(parsed) if parsed is not None else None,
             rows, error, title, meta.get('etag'), meta.get('last_modified')))
        file_id = cur.fetchone()[0]
    conn.commit()
    return {'id': file_id, 'outcome': 'saved', 's3_key': key, 'period': period, 'kind': info.get('kind'),
            'rows': rows, 'error': error, 'feed': feed, 'filename': filename, 'title': title}


def _fetch_link(conn, link: dict) -> dict:
    url = str(link.get('url') or '')
    if not url.startswith('https://'):
        raise ValueError('Link must start with https://')
    feed = link.get('feed') or 'new_car_registrations'
    if feed not in _FEED_LABELS:
        raise ValueError('Unknown feed')
    resp, data, archive = _download(url)
    headers = {k.lower(): v for k, v in (resp.headers.items() if resp is not None else [])}
    return _store(conn, feed, urllib.parse.unquote(url.rsplit('/', 1)[-1]), data, {
        'url': url, 'post_url': link.get('post_url'), 'published': link.get('published'),
        'title': link.get('title'), 'page_note': link.get('page_note'), 'archive_url': archive,
        'etag': headers.get('etag'), 'last_modified': headers.get('last-modified')})


# ---------------------------------------------------------------- rebuilding the data tables

def _rebuild(conn) -> dict:
    """Rewrite the data tables from every current file's parsed contents, in one transaction."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, kind, parsed, published, fetched_at, feed FROM ext_files "
                    "WHERE NOT superseded AND parsed IS NOT NULL ORDER BY fetched_at, id")
        files = cur.fetchall()
    regs, used, outlook, cap = [], {}, [], {}
    for file_id, kind, parsed, published, _, feed in files:
        parsed = parsed or {}
        if feed == 'cap_market_overview':
            # The most recent report for a month wins (files are oldest first).
            if parsed.get('movements') and parsed.get('month'):
                cap[parsed['month']] = (file_id, parsed)
            continue
        if parsed.get('marques') or parsed.get('tracker'):
            regs.append({'file_id': file_id, 'marques': parsed.get('marques'), 'tracker': parsed.get('tracker'),
                         'order': (0 if parsed.get('tracker') else 1, (parsed.get('marques') or {}).get('month', ''))})
        # Used car sheets: a later file restates earlier months, so the newest file's month wins.
        latest = max((u['month'] for u in parsed.get('used') or []), default='')
        for u in parsed.get('used') or []:
            old = used.get(u['month'])
            if old is None or latest >= old[2]:
                used[u['month']] = (u['units'], file_id, latest)
        for o in parsed.get('outlook') or []:
            outlook.append((file_id, parsed.get('published') or published, o['year'], o['forecast'],
                            o['measure'][:60], o['thousands']))
    # The tracker goes first, then the monthly sheets in month order, so later sheets win ties.
    regs.sort(key=lambda f: f['order'])
    combined = smmt.combine_registrations(regs)
    movements, movers, monthly = [], [], []
    for month, (file_id, parsed) in cap.items():
        for i, m in enumerate(parsed['movements']):
            movements.append((month, m['table'], m['segment'][:60], m['age_band'], m['pct'], m['partial'], file_id, i))
        for i, m in enumerate(parsed.get('movers') or []):
            movers.append((month, m['band'], m['model'][:120], m['min_gbp'], m['max_gbp'], m['avg_gbp'], file_id, i))
        monthly.append((month, parsed.get('overall_3yr_60k'), file_id))
    with conn.cursor() as cur:
        for table in ('smmt_car_totals', 'smmt_car_marques', 'smmt_used_car_sales', 'smmt_outlook',
                      'cap_value_movements', 'cap_notable_movers', 'cap_monthly'):
            cur.execute(f"DELETE FROM {table}")
        psycopg2.extras.execute_values(cur, "INSERT INTO smmt_car_totals VALUES %s", [
            (m, v['units'], v['how'], v['file_id'], v.get('private'), v.get('fleet'))
            for m, v in combined['totals'].items()])
        psycopg2.extras.execute_values(cur, "INSERT INTO smmt_car_marques VALUES %s ON CONFLICT DO NOTHING", [
            (m, name[:60], v['units'], v['how'], v['file_id']) for (m, name), v in combined['marques'].items()])
        psycopg2.extras.execute_values(cur, "INSERT INTO smmt_used_car_sales VALUES %s", [
            (m, v[0], v[1]) for m, v in used.items()])
        psycopg2.extras.execute_values(cur, "INSERT INTO smmt_outlook VALUES %s ON CONFLICT DO NOTHING", outlook)
        psycopg2.extras.execute_values(cur, "INSERT INTO cap_value_movements VALUES %s ON CONFLICT DO NOTHING", movements)
        psycopg2.extras.execute_values(cur, "INSERT INTO cap_notable_movers VALUES %s ON CONFLICT DO NOTHING", movers)
        psycopg2.extras.execute_values(cur, "INSERT INTO cap_monthly VALUES %s", monthly)
    conn.commit()
    _save_state({'last_checked': _uk_now().isoformat(timespec='seconds')})
    return {'totals': len(combined['totals']), 'marques': len(combined['marques']), 'used': len(used),
            'outlook': len(outlook), 'cap_months': len(monthly)}


# ---------------------------------------------------------------- automatic check

def _bank_holidays(state: dict) -> tuple:
    """England and Wales bank holidays from gov.uk, kept for 30 days in the check state.
    (dates, state values to save). Weekends only if gov.uk cannot be reached."""
    cached = state.get('bank_holidays') or {}
    fresh = cached.get('at') and (_uk_now() - datetime.datetime.fromisoformat(cached['at'])).days < 30
    if not fresh:
        try:
            events = requests.get('https://www.gov.uk/bank-holidays.json', timeout=15).json()['england-and-wales']['events']
            cached = {'at': _uk_now().isoformat(timespec='seconds'), 'dates': [e['date'] for e in events]}
        except Exception as e:
            logger.warning("Bank holidays not read from gov.uk: %s", e)
    dates = {datetime.date.fromisoformat(d) for d in cached.get('dates') or []}
    return dates, ({'bank_holidays': cached} if cached else {})


def _target_done(conn, source: dict, target: str) -> bool:
    """Whether the file a month_start / month_end source is looking for is already stored."""
    with conn.cursor() as cur:
        if _is_cap(source['url']):
            # All four cap hpi reports for the month, and the overview read.
            cur.execute("SELECT feed, kind, rows_loaded, parsed->>'report_type' FROM ext_files"
                        " WHERE NOT superseded AND feed IN ('cap_market_overview', 'cap_future_overview') AND period = %s",
                        (target,))
            rows = cur.fetchall()
            overview = any(r[0] == 'cap_market_overview' and r[2] > 0 for r in rows)
            future = {r[3] for r in rows if r[0] == 'cap_future_overview' and r[3]}
            return overview and len(future) >= 3
        if source['feed'] == 'new_car_registrations':
            cur.execute("SELECT 1 FROM ext_files WHERE NOT superseded AND feed = %s AND period = %s"
                        " AND kind = 'marque_sheet' LIMIT 1", (source['feed'], target))
            return cur.fetchone() is not None
        # Other feeds have no month to look for: done once a new file came in this month.
        cur.execute("SELECT 1 FROM ext_files WHERE feed = %s AND fetched_at >= date_trunc('month', NOW()) LIMIT 1",
                    (source['feed'],))
        return cur.fetchone() is not None


def _alert(found: list, by: str = 'The automatic External Data check') -> None:
    """Email the people on the cap hpi / SMMT alerts about files a check stored."""
    from . import email_alerts
    for key, files in (('cap_hpi_new_report', [f for f in found if f['feed'] in cap_hpi.FEEDS]),
                       ('smmt_new_file', [f for f in found if f['feed'] not in cap_hpi.FEEDS])):
        if not files:
            continue
        lines = [f"{_FEED_LABELS.get(f['feed'], f['feed'])} {f['period']}: {f.get('title') or f['filename']}"
                 + (f" ({f['rows']} rows read)" if f.get('rows') else '') for f in files]
        try:
            email_alerts.send_alert(key, f"{len(files)} new {'CAP HPI report' if key.startswith('cap') else 'SMMT file'}"
                                         f"{'s' if len(files) > 1 else ''}",
                                    f"{by} stored:\n\n" + "\n".join(lines)
                                    + "\n\nSee Settings > External Data.")
        except Exception as e:
            logger.error("External data alert %s not sent: %s", key, e)


def auto_run(now: datetime.datetime | None = None) -> dict:
    """The hourly automatic check, for a timer to call on PROD only (none is set up yet).
    Runs each source that is due (external_checks.due), stores new files, rebuilds the
    tables and emails the alerts."""
    now = now or _uk_now()
    cfg = _config()
    hours = cfg['auto_hours']
    if not external_checks.in_hours(now, hours['from'], hours['to']):
        return {'status': 'skipped', 'reason': 'outside check hours'}
    holder = 'auto-' + uuid.uuid4().hex[:12]
    with _conn() as conn:
        _ensure_tables(conn)
        if not _take_lease(conn, holder):
            logger.info("External data automatic check skipped: another check is running")
            return {'status': 'skipped', 'reason': 'another check is running'}
        try:
            state = _state()
            holidays, holiday_state = _bank_holidays(state)
            runs = dict(state.get('auto_runs') or {})
            found, errors, ran = [], [], []
            known = _known_urls(conn)
            for source in cfg['sources']:
                last = runs.get(source['url'], {}).get('date')
                last = datetime.date.fromisoformat(last) if last else None
                go, target = external_checks.due(source['auto'], source['auto_days'], now.date(), holidays, last)
                if not go or (target and _target_done(conn, source, target)):
                    continue
                ran.append(source['name'])
                try:
                    links = _scan_source(cfg, source, 1, known)['links']
                    for link in links:
                        try:
                            r = _fetch_link(conn, link)
                            known.add(link['url'])
                            if r['outcome'] == 'saved':
                                found.append(r)
                        except Exception as e:
                            errors.append(f"{link.get('filename')}: {e}")
                    done = bool(target and _target_done(conn, source, target))
                    runs[source['url']] = {'date': now.date().isoformat(), 'at': now.isoformat(timespec='seconds'),
                                           'target': target, 'done': done, 'new': len(links)}
                except Exception as e:
                    errors.append(f"{source['name']}: {e}")
                    runs[source['url']] = {'date': now.date().isoformat(), 'at': now.isoformat(timespec='seconds'),
                                           'target': target, 'error': str(e)[:300]}
            if ran:
                for link in _refresh_links(conn):
                    try:
                        r = _fetch_link(conn, link)
                        if r['outcome'] == 'saved':
                            found.append(r)
                    except Exception as e:
                        errors.append(f"{link.get('filename')}: {e}")
            if found:
                _rebuild(conn)
            _save_state({**holiday_state, 'auto_runs': runs,
                         'last_auto': {'at': now.isoformat(timespec='seconds'), 'ran': ran,
                                       'new': len(found), 'errors': errors[:10]}})
        finally:
            _release_lease(conn, holder)
    if found:
        _alert(found)
    logger.info("External data automatic check: ran %s, %d new files, %d errors", ran, len(found), len(errors))
    return {'status': 'ok', 'ran': ran, 'new': len(found), 'errors': errors}


# ---------------------------------------------------------------- page data

def _coverage(conn) -> dict:
    today = _uk_now().date()
    last = smmt.latest_complete_month(today)
    with conn.cursor() as cur:
        cur.execute("SELECT month, units, how FROM smmt_car_totals")
        totals = {r[0]: {'units': r[1], 'how': r[2]} for r in cur.fetchall()}
        # One colour per month: the best way any marque's figure was obtained; units = marques listed.
        cur.execute("SELECT month, how, COUNT(*) FROM smmt_car_marques GROUP BY month, how")
        marque_months = {}
        for month, how, n in cur.fetchall():
            cell = marque_months.setdefault(month, {'how': how, 'units': 0})
            cell['units'] += n
            if smmt.HOW_RANK[how] < smmt.HOW_RANK[cell['how']]:
                cell['how'] = how
        cur.execute("SELECT month, units FROM smmt_used_car_sales")
        used = {r[0]: {'units': r[1], 'how': smmt.HOW_OWN} for r in cur.fetchall()}
        cur.execute("SELECT month, pct_3yr_60k FROM cap_monthly")
        cap = {r[0]: {'units': float(r[1]) if r[1] is not None else None, 'how': smmt.HOW_OWN} for r in cur.fetchall()}

    def grid(found: dict, last_month: str):
        if not found:
            return []
        first = min(found)
        rows = smmt.coverage(found, first[:4] + '-01', last_month)
        for y in rows:
            for c in y['months']:
                c['units'] = (found.get(c['month']) or {}).get('units')
                if c['month'] < first and c['how'] is None:
                    c['how'] = 'n/a'
        return rows
    cap_last = max(cap) if cap else last
    return {'totals': grid(totals, last), 'marques': grid(marque_months, last),
            'used': grid(used, max(used) if used else last), 'cap': grid(cap, cap_last), 'latest_expected': last}


def _files(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT id, feed, period, filename, kind, source_url, post_url, published, size_bytes, fetched_at,"
                    " page_note, superseded, rows_loaded, error, headline, title, checked_at"
                    " FROM ext_files ORDER BY feed, period DESC, fetched_at DESC")
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            row = dict(zip(cols, (_iso(v) for v in r)))
            row['feed_label'] = _FEED_LABELS.get(row['feed'], row['feed'])
            out.append(row)
        return out


def _outlook_latest(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT file_id, published FROM smmt_outlook GROUP BY file_id, published "
                    "ORDER BY published DESC NULLS LAST LIMIT 1")
        top = cur.fetchone()
        if not top:
            return {}
        cur.execute("SELECT year, forecast, measure, thousands FROM smmt_outlook WHERE file_id = %s "
                    "ORDER BY year", (top[0],))
        rows = cur.fetchall()
    measures, years = [], {}
    for year, forecast, measure, value in rows:
        if measure not in measures:
            measures.append(measure)
        years.setdefault(year, {'year': year, 'forecast': forecast, 'values': {}})['values'][measure] = float(value)
    return {'file_id': top[0], 'published': _iso(top[1]), 'measures': measures, 'years': list(years.values())}


# ---------------------------------------------------------------- API

@router.get(_API)
def external_data(user: str = Depends(get_current_user)):
    _admin(user)
    try:
        with _conn() as conn:
            _ensure_tables(conn)
            out = {'status': 'ok', 'config': _config(), 'state': _state(), 'files': _files(conn),
                   'coverage': _coverage(conn), 'outlook': _outlook_latest(conn), 'feeds': _FEED_LABELS,
                   'auto_modes': _AUTO_LABELS, 'running': _lease_holder(conn)}
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(month) FROM smmt_car_marques")
                out['latest_month'] = cur.fetchone()[0]
                cur.execute("SELECT month FROM cap_monthly ORDER BY month DESC")
                out['cap_months'] = [r[0] for r in cur.fetchall()]
        return out
    except Exception as e:
        logger.error("External data read failed: %s", e)
        return {'status': 'error', 'message': str(e)}


@router.get(_API + "/month/{month}")
def external_data_month(month: str, user: str = Depends(get_current_user)):
    """Registrations by marque for one month, with the same month a year before."""
    _admin(user)
    if not re.fullmatch(r'20\d\d-\d\d', month):
        return {'status': 'error', 'message': 'Month must be YYYY-MM'}
    last = f"{int(month[:4]) - 1}{month[4:]}"
    try:
        with _conn() as conn:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(a.marque, b.marque), a.units, a.how, b.units"
                    " FROM (SELECT * FROM smmt_car_marques WHERE month = %s) a"
                    " FULL JOIN (SELECT * FROM smmt_car_marques WHERE month = %s) b ON UPPER(a.marque) = UPPER(b.marque)"
                    " ORDER BY a.units DESC NULLS LAST, b.units DESC NULLS LAST", (month, last))
                rows = [{'marque': r[0], 'units': r[1], 'how': r[2], 'last_year': r[3]} for r in cur.fetchall()]
                cur.execute("SELECT month, units, how, private_units, fleet_units FROM smmt_car_totals"
                            " WHERE month IN (%s, %s)", (month, last))
                totals = {r[0]: {'units': r[1], 'how': r[2], 'private': r[3], 'fleet': r[4]} for r in cur.fetchall()}
        return {'status': 'ok', 'month': month, 'rows': rows, 'total': totals.get(month), 'total_last_year': totals.get(last)}
    except Exception as e:
        logger.error("External data month read failed: %s", e)
        return {'status': 'error', 'message': str(e)}


@router.get(_API + "/cap/{month}")
def external_data_cap(month: str, user: str = Depends(get_current_user)):
    """One cap hpi report month: average value movements and notable movers."""
    _admin(user)
    if not re.fullmatch(r'20\d\d-\d\d', month):
        return {'status': 'error', 'message': 'Month must be YYYY-MM'}
    try:
        with _conn() as conn:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT tbl, segment, age_band, pct, partial FROM cap_value_movements WHERE month = %s"
                            " ORDER BY seq", (month,))
                movements = [{'table': r[0], 'segment': r[1], 'age_band': r[2],
                              'pct': float(r[3]) if r[3] is not None else None, 'partial': r[4]} for r in cur.fetchall()]
                cur.execute("SELECT band, model, min_gbp, max_gbp, avg_gbp FROM cap_notable_movers WHERE month = %s"
                            " ORDER BY seq", (month,))
                movers = [{'band': r[0], 'model': r[1], 'min': float(r[2]) if r[2] is not None else None,
                           'max': float(r[3]) if r[3] is not None else None,
                           'avg': float(r[4]) if r[4] is not None else None} for r in cur.fetchall()]
                cur.execute("SELECT f.id, f.title, f.filename FROM cap_monthly m JOIN ext_files f ON f.id = m.file_id"
                            " WHERE m.month = %s", (month,))
                f = cur.fetchone()
        return {'status': 'ok', 'month': month, 'movements': movements, 'movers': movers,
                'bands': list(cap_hpi.AGE_BANDS), 'file': {'id': f[0], 'title': f[1], 'filename': f[2]} if f else None}
    except Exception as e:
        logger.error("External data cap hpi read failed: %s", e)
        return {'status': 'error', 'message': str(e)}


@router.post(_API + "/config")
def external_data_config(payload: dict, user: str = Depends(get_current_user)):
    """Save the sources (with their automatic check), skip words, check hours, home news switches and colours."""
    _admin(user)
    payload = payload or {}
    values = {}
    if 'sources' in payload:
        sources = []
        for s in payload['sources'] or []:
            s = _clean_source(s)
            if not s['url'].startswith('https://'):
                return {'status': 'error', 'message': f"Source link must start with https://: {s['url'] or '(blank)'}"}
            if s['feed'] not in _FEED_LABELS or s['type'] not in ('page', 'listing'):
                return {'status': 'error', 'message': f"Pick a feed and a type for {s['url']}"}
            sources.append(s)
        values['sources'] = sources
    if 'skip_words' in payload:
        values['skip_words'] = [w.strip() for w in payload['skip_words'] or [] if str(w).strip()]
    if 'auto_check_hours' in payload:
        try:
            values['auto_check_hours'] = max(0, int(payload['auto_check_hours']))
        except (TypeError, ValueError):
            return {'status': 'error', 'message': 'Check every (hours) must be a whole number'}
    if 'auto_hours' in payload:
        try:
            start, end = int(payload['auto_hours'].get('from')), int(payload['auto_hours'].get('to'))
        except (AttributeError, TypeError, ValueError):
            return {'status': 'error', 'message': 'Automatic check hours must be whole numbers'}
        if not 0 <= start < end <= 24:
            return {'status': 'error', 'message': 'Automatic check hours: From must be before To, 0 to 24'}
        values['auto_hours'] = {'from': start, 'to': end}
    for key in ('home_news_enabled', 'home_news_admin_only'):
        if key in payload:
            values[key] = bool(payload[key])
    if 'colours' in payload:
        colours = {**DEFAULT_CONFIG['colours']}
        for k, v in (payload['colours'] or {}).items():
            if k in colours and re.fullmatch(r'#[0-9a-fA-F]{6}', str(v)):
                colours[k] = v
        values['colours'] = colours
    try:
        _save_config(values)
        logger.info("External data settings saved by %s", user)
        return {'status': 'ok', 'config': _config()}
    except Exception as e:
        logger.error("External data settings save failed: %s", e)
        return {'status': 'error', 'message': str(e)}


@router.post(_API + "/lease")
def external_data_lease(payload: dict, user: str = Depends(get_current_user)):
    """Start ({'job'}) or end ({'job', 'release': true}) a check from the page."""
    _admin(user)
    job = str((payload or {}).get('job') or '')[:40]
    if not job:
        return {'status': 'error', 'message': 'No job id'}
    holder = f'{user}-{job}'
    try:
        with _conn() as conn:
            _ensure_tables(conn)
            if (payload or {}).get('release'):
                _release_lease(conn, holder)
                return {'status': 'ok'}
            if not _take_lease(conn, holder):
                running = _lease_holder(conn) or ''
                who = 'the automatic check' if running.startswith('auto-') else 'another tab or user'
                return {'status': 'error', 'message': f'A check is already running ({who}). Try again in a few minutes.'}
        return {'status': 'ok'}
    except Exception as e:
        logger.error("External data lease failed: %s", e)
        return {'status': 'error', 'message': str(e)}


def _renew(conn, user: str, payload: dict) -> None:
    job = str((payload or {}).get('job') or '')[:40]
    if job:
        _take_lease(conn, f'{user}-{job}')


@router.post(_API + "/scan")
def external_data_scan(payload: dict, user: str = Depends(get_current_user)):
    """Scan one source (one listing page at a time) for files not stored yet.

    Returns {'links': [...], 'next_page': n | None}. The page calls this again with
    next_page until it is None; Check For New Data only asks for page 1."""
    _admin(user)
    payload = payload or {}
    cfg = _config()
    try:
        source = cfg['sources'][int(payload.get('source', 0))]
    except (IndexError, TypeError, ValueError):
        return {'status': 'error', 'message': 'No such source'}
    page = max(1, int(payload.get('page') or 1))
    try:
        with _conn() as conn:
            _ensure_tables(conn)
            _renew(conn, user, payload)
            known = _known_urls(conn)
        res = _scan_source(cfg, source, page, known)
        return {'status': 'ok', **res, 'source': source['name']}
    except Exception as e:
        logger.error("External data scan of %s failed: %s", source.get('url'), e)
        return {'status': 'error', 'message': f"{source['name']}: {e}"}


@router.post(_API + "/refresh")
def external_data_refresh(payload: dict, user: str = Depends(get_current_user)):
    """Recent files that need downloading again to see whether the publisher replaced them."""
    _admin(user)
    try:
        with _conn() as conn:
            _ensure_tables(conn)
            _renew(conn, user, payload)
            return {'status': 'ok', 'links': _refresh_links(conn)}
    except Exception as e:
        logger.error("External data refresh check failed: %s", e)
        return {'status': 'error', 'message': str(e)}


@router.post(_API + "/fetch")
def external_data_fetch(payload: dict, user: str = Depends(get_current_user)):
    """Download one linked file and store it. 'outcome' says whether it was new."""
    _admin(user)
    payload = payload or {}
    try:
        with _conn() as conn:
            _ensure_tables(conn)
            _renew(conn, user, payload)
            result = _fetch_link(conn, payload)
        return {'status': 'ok', **result}
    except Exception as e:
        logger.error("External data fetch of %s failed: %s", payload.get('url'), e)
        return {'status': 'error', 'message': str(e)}


@router.post(_API + "/upload")
def external_data_upload(file: UploadFile = File(...), feed: str = Form('new_car_registrations'),
                         user: str = Depends(get_current_user)):
    """Store a file from Tom's computer (old releases, trackers, reports)."""
    _admin(user)
    if feed not in _FEED_LABELS:
        return {'status': 'error', 'message': 'Unknown feed'}
    try:
        data = file.file.read()
        with _conn() as conn:
            _ensure_tables(conn)
            result = _store(conn, feed, file.filename or 'upload', data, {'title': file.filename})
        return {'status': 'ok', **result}
    except Exception as e:
        logger.error("External data upload failed: %s", e)
        return {'status': 'error', 'message': str(e)}


@router.post(_API + "/notify")
def external_data_notify(payload: dict, user: str = Depends(get_current_user)):
    """Send the CAP HPI / SMMT email alerts for files a check from the page just saved."""
    _admin(user)
    ids = [int(i) for i in (payload or {}).get('file_ids') or [] if str(i).isdigit()][:200]
    if not ids:
        return {'status': 'ok', 'sent': 0}
    try:
        with _conn() as conn:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT feed, period, title, filename, rows_loaded FROM ext_files WHERE id = ANY(%s)", (ids,))
                found = [{'feed': r[0], 'period': r[1], 'title': r[2], 'filename': r[3], 'rows': r[4]}
                         for r in cur.fetchall()]
        _alert(found, 'A check from Settings > External Data')
        return {'status': 'ok', 'sent': len(found)}
    except Exception as e:
        logger.error("External data alert failed: %s", e)
        return {'status': 'error', 'message': str(e)}


@router.post(_API + "/rebuild")
def external_data_rebuild(user: str = Depends(get_current_user)):
    """Rewrite the data tables from the stored files, and record the check time."""
    _admin(user)
    try:
        with _conn() as conn:
            _ensure_tables(conn)
            counts = _rebuild(conn)
        return {'status': 'ok', 'counts': counts}
    except Exception as e:
        logger.error("External data rebuild failed: %s", e)
        return {'status': 'error', 'message': str(e)}


def _split_key(key: str):
    """'<root>/<publisher>/<feed>/<period>/<file>' -> (feed, period, file), else None."""
    if not key.startswith(smmt.EXTERNAL_ROOT + '/'):
        return None
    parts = key[len(smmt.EXTERNAL_ROOT) + 1:].split('/')
    if len(parts) != 4 or parts[1] not in _FEED_LABELS or parts[0] != smmt.publisher_for(parts[1]):
        return None
    return parts[1], parts[2], parts[3]


@router.get(_API + "/s3-missing")
def external_data_s3_missing(user: str = Depends(get_current_user)):
    """Files in the S3 folders that have no row here (e.g. put there by hand, or a new database)."""
    _admin(user)
    try:
        keys = []
        for page in _s3().get_paginator('list_objects_v2').paginate(Bucket=s3_bucketname, Prefix=smmt.EXTERNAL_ROOT + '/'):
            keys += [o['Key'] for o in page.get('Contents', []) if _split_key(o['Key'])]
        with _conn() as conn:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT s3_key FROM ext_files")
                have = {r[0] for r in cur.fetchall()}
        return {'status': 'ok', 'keys': [k for k in keys if k not in have], 'total': len(keys)}
    except Exception as e:
        logger.error("External data S3 list failed: %s", e)
        return {'status': 'error', 'message': str(e)}


@router.post(_API + "/s3-load")
def external_data_s3_load(payload: dict, user: str = Depends(get_current_user)):
    """Record one file that is already in S3, using the metadata saved with it."""
    _admin(user)
    key = str((payload or {}).get('key') or '')
    parts = _split_key(key)
    if not parts:
        return {'status': 'error', 'message': f'Not an External Data file key: {key}'}
    feed, period, filename = parts
    try:
        obj = _s3().get_object(Bucket=s3_bucketname, Key=key)
        data = obj['Body'].read()
        meta = {k: urllib.parse.unquote(v) for k, v in (obj.get('Metadata') or {}).items()}
        info, parsed, rows, _ = _read_file(feed, filename, data, meta.get('title'), meta.get('published'))
        with _conn() as conn:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ext_files (s3_key, feed, period, filename, kind, source_url, post_url, published,"
                    " sha256, size_bytes, fetched_at, page_note, headline, parsed, rows_loaded, title)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (s3_key) DO NOTHING",
                    (key, feed, period, filename, info.get('kind'), meta.get('source_url'), meta.get('post_url'),
                     meta.get('published') or None, hashlib.sha256(data).hexdigest(), len(data),
                     meta.get('fetched_at') or _uk_now(), meta.get('page_note'),
                     info.get('headline') or None, psycopg2.extras.Json(parsed), rows, meta.get('title')))
            conn.commit()
        return {'status': 'ok', 'kind': info.get('kind'), 'rows': rows}
    except Exception as e:
        logger.error("External data S3 load of %s failed: %s", key, e)
        return {'status': 'error', 'message': str(e)}


@router.get(_API + "/file/{file_id}")
def external_data_file(file_id: int, user: str = Depends(get_current_user)):
    """Download a stored raw file (a short-lived S3 link)."""
    _admin(user)
    with _conn() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT s3_key, filename FROM ext_files WHERE id = %s", (file_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No such file")
    url = _s3().generate_presigned_url('get_object', ExpiresIn=600, Params={
        'Bucket': s3_bucketname, 'Key': row[0],
        'ResponseContentDisposition': f'attachment; filename="{row[1]}"'})
    return RedirectResponse(url=url, status_code=303)
