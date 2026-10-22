"""Manual API requests: run an ingestion pull by hand from the website.

Settings > API Requests uses these routes, and any other page can start a run
with static/js/api_requests.js. Each API is a "source" in SOURCES below; Nexus
Point is the first. To add another (e.g. Autotrader), write its fetch-and-save
function in utils/, add it to SOURCES, and add its form to the API Requests
section of settings.html.

A run is a job, like Copy Prod To Dev: the page starts it and polls the log.
Each endpoint gets its own Lambda invocation, one after another, so the
10-minute Lambda limit applies per endpoint rather than to the whole run, and
only one endpoint hits the API at a time.
"""
import datetime
import json
import logging
import os
import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException

from utils.db_utils import get_database_connection, save_settings_partial
from utils import nexus_ingest
from .core import BASE_URL, get_current_user, ADMIN_USERNAME

logger = logging.getLogger(__name__)

router = APIRouter()

# A running job whose worker has not logged anything for this long has been
# cut off (Lambda timeout is 10 minutes) and is marked failed.
STALE_SECONDS = 11 * 60


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _parse_updated_since(value: str):
    """'2026-10-19T08:30' from the page -> '2026-10-19 08:30:00'. UK local
    time, which is what the Nexus updated_at filter expects."""
    value = str(value or '').strip()
    if not value:
        return None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.datetime.strptime(value, fmt).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
    raise ValueError("Updated Since must be a date and time")


def _nexus_params(data: dict, defaults: dict) -> dict:
    """Check the page's values. Raises ValueError with a message for the page."""
    host = str(data.get('host') or defaults['nexus_host']).strip().rstrip('/')
    if not host.startswith('https://') or len(host) > 250:
        raise ValueError("API Host must start with https://")
    return {
        'host':           host,
        'updated_since':  _parse_updated_since(data.get('updated_since')),
        'per_page':       min(100, max(1, int(data.get('per_page') or defaults['nexus_per_page']))),
        'workers':        min(10, max(1, int(data.get('workers') or defaults['nexus_workers']))),
        'lookback_hours': min(720, max(1, int(data.get('lookback_hours') or defaults['nexus_lookback_hours']))),
        'folder':         data.get('folder') if data.get('folder') in ('v0', 'dev') else 'v0',
    }


def _nexus_remember(p: dict) -> dict:
    """Settings saved from a run, shown as the defaults next time."""
    return {'nexus_host': p['host'], 'nexus_per_page': p['per_page'],
            'nexus_workers': p['workers'], 'nexus_lookback_hours': p['lookback_hours']}


def _nexus_describe(p: dict) -> str:
    return (f"updated since {p['updated_since'] or 'the start (all rows)'}, "
            f"{p['per_page']} per page, {p['workers']} workers, to {p['folder']}")


def _nexus_run_route(route: str, p: dict, note, stop_requested) -> dict:
    """Fetch and save one endpoint. Returns what was saved, kept on the job so
    Live Update can check it: row count, S3 file, newest updated_at."""
    rows = nexus_ingest.fetch_route(p['host'], route, p['per_page'], p['workers'], p['updated_since'],
                                    note=note, stop_requested=stop_requested)
    if not rows:
        note(f"{route}: no rows - nothing saved")
        return {'rows': 0}
    s3 = nexus_ingest.upload_route(route, rows, p['folder'])
    note(f"{route}: {len(rows)} rows saved to {s3}")
    updated = [str(r['updated_at']) for r in rows if isinstance(r, dict) and r.get('updated_at')]
    return {'rows': len(rows), 's3': s3, 'max_updated_at': max(updated) if updated else None}


SOURCES = {
    'nexus': {
        'label': 'Nexus Point',
        # Fills the endpoint list the first time it is opened; after that the
        # list is edited on the page and stored in api_request_routes.
        'default_routes': [
            'vehicles', 'invoices', 'valuations', 'invoices/invoice-finance-amounts',
            'invoices/invoice-products', 'invoices/invoice-product-discounts', 'contacts',
            'enquiries', 'enquiries/enquiry-vehicles', 'invoices/invoice-finance-offers',
            'valuations/notes', 'vehicles/notes', 'invoices/invoice-valuations',
            'invoices/invoice-part-exchange-settlements',
        ],
        'defaults': {
            'nexus_host':           'https://data-api.nexuspointapex.com',
            'nexus_per_page':       100,
            'nexus_workers':        4,
            # Updated Since is pre-filled with UK time now minus this many hours.
            'nexus_lookback_hours': 3,
        },
        'params': _nexus_params,
        'remember': _nexus_remember,
        'describe': _nexus_describe,
        'run_route': _nexus_run_route,
        'stopped_error': nexus_ingest.FetchStopped,
        # The S3 folder dbt reads for an endpoint - matched against the raw
        # models' stage_url to find which models an endpoint feeds.
        'stage_path': lambda route: f"nexus_point/{route}/json/v0/",
    },
}


def _source(source: str) -> dict:
    if source not in SOURCES:
        raise HTTPException(status_code=404, detail="Unknown source")
    return SOURCES[source]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _admin(user: str):
    if user != ADMIN_USERNAME:
        raise HTTPException(status_code=403, detail="Forbidden")


def _ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS api_request_routes ("
            " source VARCHAR(30) NOT NULL,"
            " route VARCHAR(200) NOT NULL,"
            " sort_order INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (source, route))"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS api_request_jobs ("
            " job_id VARCHAR(36) PRIMARY KEY,"
            " source VARCHAR(30) NOT NULL,"
            " status VARCHAR(10) NOT NULL,"
            " message TEXT,"
            " log TEXT[] NOT NULL DEFAULT '{}',"
            " routes TEXT[] NOT NULL,"
            " params JSONB NOT NULL,"
            " claimed_index INTEGER NOT NULL DEFAULT -1,"
            " failed_count INTEGER NOT NULL DEFAULT 0,"
            " stop_requested BOOLEAN NOT NULL DEFAULT FALSE,"
            " created_by VARCHAR(100),"
            " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " finished_at TIMESTAMPTZ)"
        )
        # Per endpoint: {route: {rows, s3, max_updated_at} or {error}}.
        cur.execute("ALTER TABLE api_request_jobs ADD COLUMN IF NOT EXISTS results JSONB NOT NULL DEFAULT '{}'")
        for source, cfg in SOURCES.items():
            cur.execute("SELECT 1 FROM api_request_routes WHERE source = %s LIMIT 1", (source,))
            if cur.fetchone() is None:
                for i, route in enumerate(cfg['default_routes']):
                    cur.execute("INSERT INTO api_request_routes (source, route, sort_order) "
                                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (source, route, i * 10))
    conn.commit()


def _load_config(conn, source: str) -> dict:
    defaults = _source(source)['defaults']
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM settings WHERE key = ANY(%s)", (list(defaults),))
        stored = dict(cur.fetchall())
        cur.execute("SELECT route FROM api_request_routes WHERE source = %s ORDER BY sort_order, route",
                    (source,))
        routes = [r[0] for r in cur.fetchall()]
    config = dict(defaults)
    for key, default in defaults.items():
        if key in stored:
            try:
                config[key] = int(stored[key]) if isinstance(default, int) else stored[key]
            except ValueError:
                pass
    config['routes'] = routes
    return config


def _clean_route(route: str) -> str:
    return '/'.join(s for s in str(route or '').strip().strip('/').split('/') if s).lower()


def _mark_stale(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_request_jobs SET status = 'failed', finished_at = NOW(), "
            " message = 'The worker stopped without finishing - an endpoint probably ran past the "
            "10-minute Lambda limit. Try a shorter time window.' "
            "WHERE status IN ('queued', 'running') AND heartbeat_at < NOW() - %s * INTERVAL '1 second'",
            (STALE_SECONDS,))
    conn.commit()


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@router.get("/api/api-requests/sources")
def api_sources(user: str = Depends(get_current_user)):
    _admin(user)
    return {"status": "ok", "sources": [{"key": k, "label": v['label']} for k, v in SOURCES.items()]}


@router.get("/api/api-requests/{source}/config")
def api_config(source: str, user: str = Depends(get_current_user)):
    _admin(user)
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        config = _load_config(conn, source)
        with conn.cursor() as cur:
            cur.execute("SELECT job_id FROM api_request_jobs WHERE source = %s "
                        "AND status IN ('queued', 'running') ORDER BY created_at DESC LIMIT 1", (source,))
            row = cur.fetchone()
        conn.rollback()
    return {"status": "ok", **config, "open_job_id": row[0] if row else None}


@router.post("/api/api-requests/{source}/routes")
def api_add_route(source: str, data: dict, user: str = Depends(get_current_user)):
    _admin(user)
    _source(source)
    route = _clean_route(data.get('route'))
    if not route or len(route) > 200:
        return {"status": "error", "message": "Enter an endpoint, e.g. invoices/invoice-products"}
    with get_database_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO api_request_routes (source, route, sort_order) "
                        "SELECT %s, %s, COALESCE(MAX(sort_order), 0) + 10 FROM api_request_routes "
                        "WHERE source = %s ON CONFLICT DO NOTHING", (source, route, source))
            added = cur.rowcount == 1
        conn.commit()
    if not added:
        return {"status": "error", "message": f"{route} is already in the list"}
    logger.info("API requests [%s]: %s added by %s", source, route, user)
    return {"status": "ok"}


@router.post("/api/api-requests/nexus/routes/swagger")
def api_routes_from_swagger(user: str = Depends(get_current_user)):
    """Add every list endpoint in the Nexus API's swagger.json that is not in
    the list yet, and remember which take no updated_at filter (those pull
    every row on each run - Live Update warns about them)."""
    _admin(user)
    import requests
    from utils.secrets_manager_client import get_secret
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        host = _load_config(conn, 'nexus')['nexus_host']
        conn.rollback()
    try:
        resp = requests.get(f"{host.rstrip('/')}/swagger.json", timeout=30, headers={
            'Authorization': f"Bearer {get_secret(nexus_ingest.NEXUS_SECRET_NAME)['access_token']}"})
        resp.raise_for_status()
        paths = resp.json().get('paths') or {}
    except Exception as e:
        return {"status": "error", "message": f"Could not read {host}/swagger.json: {str(e)[:300]}"}
    found, no_filter = [], []
    for path, ops in paths.items():
        get = (ops or {}).get('get') if isinstance(ops, dict) else None
        if not get or '{' in path:
            continue   # only plain list endpoints, not /vehicles/adverts/{advert_id}
        route = _clean_route(path)
        if not route:
            continue
        found.append(route)
        params = {p.get('name') for p in (get.get('parameters') or []) if isinstance(p, dict)}
        if 'updated_at' not in params:
            no_filter.append(route)
    added = []
    with get_database_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            for route in found:
                cur.execute("INSERT INTO api_request_routes (source, route, sort_order) "
                            "SELECT 'nexus', %s, COALESCE(MAX(sort_order), 0) + 10 FROM api_request_routes "
                            "WHERE source = 'nexus' ON CONFLICT DO NOTHING", (route,))
                if cur.rowcount == 1:
                    added.append(route)
        conn.commit()
        save_settings_partial(conn, {'nexus_no_updated_at': json.dumps(sorted(no_filter))})
    logger.info("API requests [nexus]: %s endpoints added from swagger by %s", len(added), user)
    return {"status": "ok", "found": len(found), "added": added, "no_updated_at": sorted(no_filter)}


@router.post("/api/api-requests/{source}/routes/delete")
def api_delete_route(source: str, data: dict, user: str = Depends(get_current_user)):
    _admin(user)
    route = _clean_route(data.get('route'))
    with get_database_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM api_request_routes WHERE source = %s AND route = %s", (source, route))
        conn.commit()
    logger.info("API requests [%s]: %s removed by %s", source, route, user)
    return {"status": "ok"}


@router.post("/api/api-requests/{source}/start")
def api_start(source: str, data: dict, user: str = Depends(get_current_user)):
    _admin(user)
    cfg = _source(source)
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        known = set(_load_config(conn, source)['routes'])
    routes = [r for r in (_clean_route(x) for x in data.get('routes') or []) if r in known]
    if not routes:
        return {"status": "error", "message": "Tick at least one endpoint"}
    try:
        params = cfg['params'](data, cfg['defaults'])
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    with get_database_connection() as conn:
        conn.rollback()
        from .live_update import open_live_update
        busy = open_live_update(conn)
        if busy:
            return {"status": "error", "message": f"Live Update {busy} is going - wait for it or stop it first."}
        save_settings_partial(conn, cfg['remember'](params))
        try:
            job_id = create_job(conn, source, routes, params, user)
        except ValueError as e:
            return {"status": "error", "message": str(e)}
    _dispatch(job_id, 0)
    return {"status": "started", "job_id": job_id}


def create_job(conn, source: str, routes: list, params: dict, user: str) -> str:
    """Add a queued job and return its id; the caller starts it with
    _dispatch(job_id, 0) once committed. ValueError if one is already going."""
    cfg = _source(source)
    _ensure_tables(conn)
    _mark_stale(conn)
    job_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM api_request_jobs WHERE source = %s "
                    "AND status IN ('queued', 'running')", (source,))
        if cur.fetchone()[0]:
            conn.rollback()
            raise ValueError(f"A {cfg['label']} run is already going - press Stop first.")
        cur.execute(
            "INSERT INTO api_request_jobs (job_id, source, status, routes, params, created_by) "
            "VALUES (%s, %s, 'queued', %s, %s, %s)",
            (job_id, source, routes, json.dumps(params), user))
    conn.commit()
    logger.info("API requests [%s] job %s started by %s: %s, %s",
                source, job_id, user, ', '.join(routes), cfg['describe'](params))
    return job_id


def load_job(conn, job_id: str):
    """The job as Live Update needs it, or None."""
    _mark_stale(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT status, message, log, results, routes, claimed_index FROM api_request_jobs "
                    "WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    conn.rollback()
    if not row:
        return None
    results = row[3] if isinstance(row[3], dict) else json.loads(row[3] or '{}')
    return {'status': row[0], 'message': row[1] or '', 'log': list(row[2] or []), 'results': results,
            'routes': list(row[4] or []), 'claimed_index': row[5]}


@router.get("/api/api-requests/job/{job_id}")
def api_job(job_id: str, user: str = Depends(get_current_user)):
    _admin(user)
    with get_database_connection() as conn:
        conn.rollback()
        _mark_stale(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT status, message, log FROM api_request_jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
        conn.rollback()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": row[0], "message": row[1] or "", "log": list(row[2] or [])}


@router.post("/api/api-requests/{source}/stop")
def api_stop(source: str, user: str = Depends(get_current_user)):
    """Ask the source's open run to stop. The worker notices between progress
    lines and between endpoints; the endpoint it was on is not saved."""
    _admin(user)
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE api_request_jobs SET stop_requested = TRUE "
                        "WHERE source = %s AND status IN ('queued', 'running') RETURNING job_id", (source,))
            jobs = cur.fetchall()
        conn.commit()
    if not jobs:
        return {"status": "ok", "message": "Nothing was running"}
    return {"status": "ok", "message": "Stopping - the current endpoint will not be saved"}


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _dispatch(job_id: str, index: int) -> None:
    """Start the worker for one endpoint: its own Lambda invocation on Lambda
    (a thread is frozen once the response is sent), a thread locally."""
    function_name = os.getenv("AWS_LAMBDA_FUNCTION_NAME")
    if os.getenv("LOCAL_ENV") == "1" or not function_name:
        threading.Thread(target=_run_safely, args=(job_id, index), daemon=True,
                         name=f"api-request-{job_id[:8]}-{index}").start()
        return
    try:
        from types import SimpleNamespace
        from routers.core import worker_secret
        from utils.lambda_http import LambdaRequestsManager
        LambdaRequestsManager().simulate_http(
            aws_context=SimpleNamespace(function_name=function_name),
            path=BASE_URL + "/api/api-requests/run",
            method="POST",
            data={"job_id": job_id, "index": index, "secret": worker_secret()},
        )
    except Exception as e:
        logger.exception("API requests job %s could not start endpoint %s", job_id, index)
        _finish(job_id, "failed", f"Could not start the next endpoint: {str(e)[:300]}")


@router.post("/api/api-requests/run")
def api_run(data: dict):
    """Internal worker route, invoked by our own Lambda."""
    from routers.core import check_worker_secret
    check_worker_secret(data.get("secret"))
    job_id = str(data.get("job_id") or "")
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id missing")
    _run_safely(job_id, int(data.get("index") or 0))
    return {"ok": True}


def _log(job_id: str, line: str) -> None:
    """Add a log line and refresh the heartbeat."""
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE api_request_jobs SET log = log || %s::text[], heartbeat_at = NOW() "
                        "WHERE job_id = %s", ([line], job_id))
        conn.commit()


def _save_result(job_id: str, route: str, result: dict) -> None:
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE api_request_jobs SET results = results || jsonb_build_object(%s, %s::jsonb) "
                        "WHERE job_id = %s", (route, json.dumps(result, default=str), job_id))
        conn.commit()


def _stop_requested(job_id: str) -> bool:
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT stop_requested FROM api_request_jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
        conn.rollback()
    return bool(row and row[0])


def _finish(job_id: str, status: str, message: str) -> None:
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE api_request_jobs SET status = %s, message = %s, finished_at = NOW() "
                        "WHERE job_id = %s AND status IN ('queued', 'running')",
                        (status, message[:1000], job_id))
        conn.commit()


def _claim(job_id: str, index: int):
    """Take endpoint `index` of the job, once. A repeated delivery of the same
    invocation finds claimed_index already moved on and does nothing."""
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE api_request_jobs SET status = 'running', claimed_index = %s, heartbeat_at = NOW() "
                "WHERE job_id = %s AND status IN ('queued', 'running') AND claimed_index < %s "
                "RETURNING source, routes, params, stop_requested",
                (index, job_id, index))
            row = cur.fetchone()
        conn.commit()
    return row


def _run_safely(job_id: str, index: int) -> None:
    try:
        _run_one(job_id, index)
    except Exception as e:
        logger.exception("API requests job %s crashed", job_id)
        try:
            _finish(job_id, "failed", f"Crashed: {str(e)[:300]}")
        except Exception:
            pass


def _run_one(job_id: str, index: int) -> None:
    """Fetch and save one endpoint, then hand on to the next."""
    row = _claim(job_id, index)
    if not row:
        return
    source, routes, params, stop = row
    if isinstance(params, str):
        params = json.loads(params)
    cfg = _source(source)
    if stop:
        _finish(job_id, "stopped", "Stopped by you")
        return
    route = routes[index]
    started = time.time()

    def note(text):
        _log(job_id, f"{time.time() - started:6.1f}s  {text}")

    note(f"[{index + 1}/{len(routes)}] {cfg['label']} {route} - {cfg['describe'](params)}")
    try:
        result = cfg['run_route'](route, params, note, lambda: _stop_requested(job_id))
        _save_result(job_id, route, result or {})
    except cfg['stopped_error']:
        note(f"{route}: stopped part way - nothing saved for it")
        _finish(job_id, "stopped", "Stopped by you")
        return
    except Exception as e:
        # As the ingestion jobs do: a failed endpoint is skipped and the rest still run.
        logger.exception("API requests job %s: %s failed", job_id, route)
        note(f"{route}: FAILED - {e}. Nothing saved for it")
        _save_result(job_id, route, {'error': str(e)[:500]})
        with get_database_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE api_request_jobs SET failed_count = failed_count + 1 WHERE job_id = %s",
                            (job_id,))
            conn.commit()

    if index + 1 < len(routes):
        if _stop_requested(job_id):
            _finish(job_id, "stopped", "Stopped by you")
            return
        _dispatch(job_id, index + 1)
        return
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT failed_count FROM api_request_jobs WHERE job_id = %s", (job_id,))
            failed = cur.fetchone()[0]
        conn.rollback()
    if failed:
        _finish(job_id, "failed", f"Finished with {failed} of {len(routes)} endpoints failed - see the log")
    else:
        _finish(job_id, "done", f"Finished {len(routes)} endpoints")
