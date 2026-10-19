"""Settings page, user management and app settings (admin only)."""

from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import datetime
import time
import hashlib
import logging
import re
import zoneinfo


from utils.db_utils import (get_database_connection, get_settings, save_settings,
                            save_settings_partial, get_last_login_per_user,
                            get_active_sessions_all_envs, upsert_user,
                            load_user_details, update_user_details,
                            load_all_page_access, set_page_access_many,
                            delete_user_and_access, ALWAYS_ENABLED_USERS, ADMIN_DETAILS,
                            get_prod_pricer_connection, load_page_names, save_page_names)

from . import core
from .core import (BASE_URL, templates, VALID_USERS, _active_sessions, get_current_user,
                   ADMIN_USERNAME)
from .page_registry import (PAGES, ALL_PAGE_KEYS, CONFIGURED_MARKER, DEFAULT_PAGE_KEYS)

logger = logging.getLogger(__name__)

router = APIRouter()


# Settings sections. Each renders its own page with its own Save button, and
# each save writes only that section's keys (see save_settings_partial) so one
# page cannot overwrite fields it never displayed.
# slug -> (nav label, page heading)
SETTINGS_SECTIONS = [
    ("buying-requirements", "Buying Requirements", "Buying Requirements"),
    ("supplier-evaluator",  "Supplier Evaluator",  "Supplier Stock Evaluator"),
    ("mot-check",           "MOT Check",           "MOT Check"),
    ("kpi",                 "KPI Dashboard",       "KPI Dashboard"),
    ("cap-credentials",     "CAP Credentials",     "CAP Credential Override"),
    ("email",               "Email",               "Email"),
    ("live-users",          "Live Users",          "Live Users"),
    ("sharepoint",          "SharePoint",          "SharePoint Files"),
    ("prod-to-dev",         "Prod To Dev",         "Copy Prod To Dev"),
    ("api-requests",        "API Requests",        "API Requests"),
    ("dbt",                 "Run dbt",             "Run dbt"),
]

# The settings 'user' scope used by this admin page. The buying requirements
# page reads its own per-user values, but the admin page has always edited the
# shared 'buyer' scope — keep that unchanged.
_SETTINGS_SCOPE = 'buyer'


def _render_section(request: Request, user: str, slug: str):
    with get_database_connection() as conn:
        settings = get_settings(conn, user=_SETTINGS_SCOPE)
    section = next((s for s in SETTINGS_SECTIONS if s[0] == slug), None)
    if section is None:
        return RedirectResponse(url=BASE_URL + "/settings-page", status_code=303)
    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "settings": settings,
        "sections": SETTINGS_SECTIONS,
        "active_section": slug,
        "section_title": section[2],
    })


@router.get("/settings-page", response_class=HTMLResponse)
def settings_page(request: Request, user: str = Depends(get_current_user)):
    """Landing on the settings area — show the first section."""
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    return _render_section(request, user, SETTINGS_SECTIONS[0][0])


@router.get("/settings/section/{slug}", response_class=HTMLResponse)
def settings_section(slug: str, request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    return _render_section(request, user, slug)


def _section_redirect(slug: str):
    return RedirectResponse(url=f"{BASE_URL}/settings/section/{slug}?saved=1", status_code=303)


def _per_user_key(key: str) -> str:
    """Per-user bool settings are stored suffixed; always-enabled users store nothing."""
    if _SETTINGS_SCOPE in ALWAYS_ENABLED_USERS:
        return None
    return f"{key}__{_SETTINGS_SCOPE}" if _SETTINGS_SCOPE else key


@router.post("/settings/section/buying-requirements")
def save_buying_requirements(
    request: Request,
    number_to_buy: int = Form(...),
    max_days_sales: int = Form(...),
    max_dts: int = Form(...),
    min_gpu_m: int = Form(...),
    enable_cars_bought: str = Form(default=""),
    snowflake_cache_minutes: int = Form(...),
    user: str = Depends(get_current_user),
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    with get_database_connection() as conn:
        save_settings_partial(conn, {
            'number_to_buy':            number_to_buy,
            'max_days_sales':           max_days_sales,
            'max_dts':                  max_dts,
            'min_gpu_m':                min_gpu_m,
            'enable_cars_bought':       '1' if enable_cars_bought == "on" else '0',
            'snowflake_cache_minutes':  max(0, snowflake_cache_minutes),
        })
    logger.info("Settings [Buying Requirements] updated by %s", user)
    return _section_redirect("buying-requirements")


@router.post("/settings/section/supplier-evaluator")
def save_supplier_evaluator(
    request: Request,
    cap_workers: int = Form(...),
    cap_rate_limit: int = Form(...),
    at_workers: int = Form(...),
    at_rate_limit: int = Form(...),
    valuation_cache_hours: int = Form(...),
    calc_buying_reqs: str = Form(default=""),
    get_stock: str = Form(default=""),
    user: str = Depends(get_current_user),
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    values = {
        'cap_workers':           cap_workers,
        'cap_rate_limit':        cap_rate_limit,
        'at_workers':            at_workers,
        'at_rate_limit':         at_rate_limit,
        'valuation_cache_hours': valuation_cache_hours,
    }
    calc_key = _per_user_key('calc_buying_reqs')
    stock_key = _per_user_key('get_stock')
    if calc_key:
        values[calc_key] = '1' if calc_buying_reqs == "on" else '0'
    if stock_key:
        values[stock_key] = '1' if get_stock == "on" else '0'
    with get_database_connection() as conn:
        save_settings_partial(conn, values)
    logger.info("Settings [Supplier Evaluator] updated by %s", user)
    return _section_redirect("supplier-evaluator")


@router.post("/settings/section/mot-check")
def save_mot_check(
    request: Request,
    mot_workers: int = Form(...),
    user: str = Depends(get_current_user),
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    with get_database_connection() as conn:
        save_settings_partial(conn, {'mot_workers': mot_workers})
    logger.info("Settings [MOT Check] updated by %s", user)
    return _section_redirect("mot-check")


@router.post("/settings/section/kpi")
def save_kpi(
    request: Request,
    kpi_ai_summary: str = Form(default=""),
    user: str = Depends(get_current_user),
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    with get_database_connection() as conn:
        save_settings_partial(conn, {'kpi_ai_summary': '1' if kpi_ai_summary == "on" else '0'})
    logger.info("Settings [KPI Dashboard] updated by %s", user)
    return _section_redirect("kpi")


@router.post("/settings/section/cap-credentials")
def save_cap_credentials(
    request: Request,
    cap_override_enabled: str = Form(default=""),
    cap_override_subscriber_id: str = Form(default=""),
    cap_override_password: str = Form(default=""),
    user: str = Depends(get_current_user),
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    with get_database_connection() as conn:
        save_settings_partial(conn, {
            'cap_override_enabled':       '1' if cap_override_enabled == "on" else '0',
            'cap_override_subscriber_id': cap_override_subscriber_id.strip(),
            'cap_override_password':      cap_override_password.strip(),
        })
    # Reset CAPClient singleton so the next task picks up the new creds
    from list_processor.cap_client import CAPClient
    from utils.singleton import Singleton
    Singleton._instances.pop(CAPClient, None)
    logger.info("Settings [CAP Credentials] updated by %s", user)
    return _section_redirect("cap-credentials")


@router.post("/settings/section/email")
def save_email(
    request: Request,
    email_sender: str = Form(default=""),
    email_test_recipient: str = Form(default=""),
    user: str = Depends(get_current_user),
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    with get_database_connection() as conn:
        save_settings_partial(conn, {
            'email_sender':         email_sender.strip(),
            'email_test_recipient': email_test_recipient.strip(),
        })
    logger.info("Settings [Email] updated by %s", user)
    return _section_redirect("email")


@router.post("/settings/section/live-users")
def save_live_users(
    request: Request,
    presence_enabled: str = Form(default=""),
    presence_interval_seconds: int = Form(...),
    presence_drop_off_seconds: int = Form(...),
    presence_circle_size: int = Form(...),
    presence_max_circles: int = Form(...),
    presence_border_width: int = Form(...),
    presence_gap: int = Form(...),
    presence_ring_width: int = Form(...),
    presence_ring_gap: int = Form(...),
    presence_border_colour: str = Form(...),
    presence_text_colour: str = Form(...),
    presence_more_colour: str = Form(...),
    user: str = Depends(get_current_user),
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    # Colours go into a style attribute, so only '#rrggbb' is accepted.
    colours = {
        'presence_border_colour': presence_border_colour,
        'presence_text_colour':   presence_text_colour,
        'presence_more_colour':   presence_more_colour,
    }
    for key, value in colours.items():
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', value.strip()):
            raise HTTPException(status_code=400, detail=f"{key} must be a colour like #ffffff")
        colours[key] = value.strip().lower()
    interval = max(5, presence_interval_seconds)
    with get_database_connection() as conn:
        save_settings_partial(conn, {
            'presence_enabled':          '1' if presence_enabled == "on" else '0',
            'presence_interval_seconds': interval,
            # Must outlast at least two missed heartbeats, or circles flicker.
            'presence_drop_off_seconds': max(interval * 2 + 5, presence_drop_off_seconds),
            'presence_circle_size':      min(60, max(14, presence_circle_size)),
            'presence_max_circles':      max(1, presence_max_circles),
            'presence_border_width':     min(6, max(0, presence_border_width)),
            'presence_gap':              min(20, max(0, presence_gap)),
            'presence_ring_width':       min(4, max(0, presence_ring_width)),
            'presence_ring_gap':         min(4, max(0, presence_ring_gap)),
            **colours,
        })
    # This container uses the new values straight away; others within a minute.
    core.reload_presence_config()
    logger.info("Settings [Live Users] updated by %s", user)
    return _section_redirect("live-users")


@router.post("/settings/live-users/test-photo")
def test_microsoft_photo(email: str = Form(...), user: str = Depends(get_current_user)):
    """Fetch one person's Microsoft 365 photo through Graph, to check the app
    registration has a photo permission before building on it. Also reports
    which application permissions the token carries."""
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    import base64
    import json as _json
    import requests
    from utils.email_client import _get_token
    try:
        token = _get_token()
    except Exception as e:
        return {"status": "error", "message": f"Could not get a Graph token: {str(e)[:300]}"}
    # The token's 'roles' claim lists the application permissions granted.
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        roles = _json.loads(base64.urlsafe_b64decode(payload)).get('roles', [])
    except Exception:
        roles = []
    granted = ', '.join(sorted(roles)) or 'none listed'
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/users/{email.strip()}/photo/$value",
        headers={"Authorization": f"Bearer {token}"}, timeout=20,
    )
    if resp.status_code == 200:
        mime = resp.headers.get('Content-Type', 'image/jpeg')
        image = f"data:{mime};base64," + base64.b64encode(resp.content).decode()
        return {"status": "ok", "image": image,
                "message": f"Photo found ({len(resp.content) // 1024} KB). Permissions: {granted}"}
    try:
        detail = resp.json().get('error', {}).get('message', '')
    except Exception:
        detail = resp.text[:200]
    hint = {403: "no photo permission", 404: "no photo set, or no such user"}.get(resp.status_code, "")
    return {"status": "error",
            "message": f"HTTP {resp.status_code}{' - ' + hint if hint else ''}: {detail[:200]}. "
                       f"Permissions: {granted}"}


@router.post("/settings/email/test")
def send_test_email(request: Request, user: str = Depends(get_current_user)):
    """Send a test email from the saved sender to the saved test address, to
    confirm Graph mail sending works. Uses the SAVED values, not the boxes."""
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    from utils.email_client import send_email, configured_sender
    with get_database_connection() as conn:
        settings = get_settings(conn, user=_SETTINGS_SCOPE)
    sender = configured_sender()
    to = settings.get('email_test_recipient', '').strip() or sender
    sent_at = datetime.datetime.now(zoneinfo.ZoneInfo("Europe/London")).strftime('%Y-%m-%d %H:%M:%S')
    try:
        send_email(
            to=to,
            subject=f"VLE test email - {sent_at}",
            body_html=(
                "<p>Test email from Settings &gt; Email.</p>"
                f"<p>Sent from {sender} at {sent_at} (UK).</p>"
            ),
            sender=sender,
        )
    except Exception as e:
        logger.exception("Test email failed")
        return {"status": "error", "message": str(e)[:300]}
    return {"status": "ok", "message": f"Sent from {sender} to {to} at {sent_at}"}


@router.post("/settings/prod-to-dev/test")
def test_other_env_connection(request: Request, user: str = Depends(get_current_user)):
    """Try to connect from this Lambda to the OTHER environment's Aurora
    cluster (dev from prod, prod from dev). Reads only — nothing is written."""
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    import os
    import psycopg2
    from utils.secrets_manager_client import get_secret
    this_env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    other_env = 'dev' if this_env == 'prod' else 'prod'
    secret = f'{other_env}/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials'
    started = time.time()
    try:
        data = get_secret(secret)
    except Exception as e:
        logger.exception("Reading %s secret failed", other_env)
        return {"status": "error", "message": f"Could not read the {other_env} secret: {str(e)[:300]}"}
    try:
        conn = psycopg2.connect(
            host=data['endpoint'], user=data['username'], password=data['password'],
            database='carsupermarket_pricer', port=int(data.get('port', 5432)),
            connect_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.kpi_layout') IS NOT NULL")
                has_layout = cur.fetchone()[0]
                layout_rows = None
                if has_layout:
                    cur.execute("SELECT COUNT(*) FROM kpi_layout")
                    layout_rows = cur.fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        logger.exception("Connecting to %s Aurora failed", other_env)
        return {"status": "error",
                "message": f"{this_env} could not connect to {other_env} Aurora ({data['endpoint']}) "
                           f"after {time.time() - started:.1f}s: {str(e)[:300]}"}
    rows_text = f"{layout_rows} kpi_layout rows" if has_layout else "no kpi_layout table"
    return {"status": "ok",
            "message": f"{this_env} connected to {other_env} Aurora ({data['endpoint']}) "
                       f"in {time.time() - started:.1f}s - {rows_text}"}


# Tables copied by Copy Prod To Dev, parents before children (subsidy
# benchmarks point at schedules). User tables (user_preferences,
# user_page_access, page_presence) are deliberately left out.
PROD_TO_DEV_TABLES = [
    'settings',
    'kpi_layout',
    'chart_targets',
    'page_names',
    'subsidy_rate_schedules',
    'subsidy_rate_benchmarks',
    'sharepoint_sources',
    'ga4_campaigns',
]


def _table_columns(cur, table: str) -> list:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def _table_key_columns(cur, table: str) -> list:
    """Primary key columns, used to name each copied row in the log."""
    cur.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = to_regclass(%s) AND i.indisprimary "
        "ORDER BY array_position(i.indkey, a.attnum)",
        (f'public.{table}',),
    )
    return [r[0] for r in cur.fetchall()]


# Copy Prod To Dev runs as a job, so the page never waits on one long request
# (API Gateway cuts those off at 29s). Each log line is committed to
# prod_to_dev_jobs as it happens, so whichever Lambda answers the page's poll
# can show it. Same pattern as the SharePoint refresh jobs.
def _ensure_prod_to_dev_jobs(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS prod_to_dev_jobs ("
            " job_id VARCHAR(36) PRIMARY KEY,"
            " status VARCHAR(10) NOT NULL,"
            " message TEXT,"
            " log TEXT[] NOT NULL DEFAULT '{}',"
            " created_by VARCHAR(100),"
            " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " finished_at TIMESTAMPTZ)"
        )
        # backend_pid: the dev session doing the copy, so Stop can cancel it.
        cur.execute("ALTER TABLE prod_to_dev_jobs ADD COLUMN IF NOT EXISTS backend_pid INTEGER")
        cur.execute("ALTER TABLE prod_to_dev_jobs ADD COLUMN IF NOT EXISTS "
                    "stop_requested BOOLEAN NOT NULL DEFAULT FALSE")
    conn.commit()


class _CopyStopped(Exception):
    pass


def _job_log(job_id: str, lines) -> None:
    """Add one log line, or a list of them in one save. Raises _CopyStopped
    if Stop has been pressed."""
    if isinstance(lines, str):
        lines = [lines]
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE prod_to_dev_jobs SET log = log || %s::text[] WHERE job_id = %s "
                        "RETURNING stop_requested", (list(lines), job_id))
            row = cur.fetchone()
        conn.commit()
    if row and row[0]:
        raise _CopyStopped()


def _job_stop_requested(job_id: str) -> bool:
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT stop_requested FROM prod_to_dev_jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
        conn.rollback()
    return bool(row and row[0])


def _lock_holders(table: str, own_pid: int) -> list:
    """Other dev sessions holding a lock on the table, to say what blocked the copy."""
    with get_database_connection() as conn:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT a.pid, a.state, a.application_name, a.client_addr::text, "
                "       EXTRACT(EPOCH FROM NOW() - a.xact_start)::int, LEFT(a.query, 150) "
                "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.relation = to_regclass(%s) AND l.granted AND a.pid <> %s",
                (f'public.{table}', own_pid),
            )
            rows = cur.fetchall()
        conn.rollback()
    return [f"pid {r[0]}, {r[1]}, app '{r[2] or ''}', from {r[3] or 'local'}, "
            f"transaction open {r[4]}s, last query: {r[5]}" for r in rows]


def _job_finish(job_id: str, status: str, message: str) -> None:
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE prod_to_dev_jobs SET status = %s, message = %s, finished_at = NOW() "
                        "WHERE job_id = %s AND status IN ('queued', 'running')",
                        (status, message[:1000], job_id))
        conn.commit()


def _job_claim(job_id: str) -> bool:
    """queued -> running in one statement, so a repeated delivery does nothing."""
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE prod_to_dev_jobs SET status = 'running' "
                        "WHERE job_id = %s AND status = 'queued'", (job_id,))
            won = cur.rowcount == 1
        conn.commit()
    return won


@router.post("/settings/prod-to-dev/copy")
def copy_prod_to_dev(request: Request, user: str = Depends(get_current_user)):
    """Start a Copy Prod To Dev job and return its id for the page to poll."""
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    import os
    import uuid
    import threading
    if os.getenv('AWS_REGION') == 'eu-west-1':
        return {"status": "error", "message": "This only runs on dev - it would overwrite prod with itself."}
    job_id = str(uuid.uuid4())
    try:
        with get_database_connection() as conn:
            conn.rollback()
            try:
                # Fail fast rather than hang if something holds a lock.
                with conn.cursor() as cur:
                    cur.execute("SET lock_timeout = '5s'")
                _ensure_prod_to_dev_jobs(conn)
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM prod_to_dev_jobs WHERE status IN ('queued', 'running')")
                    open_jobs = cur.fetchone()[0]
                    if not open_jobs:
                        cur.execute("INSERT INTO prod_to_dev_jobs (job_id, status, created_by) "
                                    "VALUES (%s, 'queued', %s)", (job_id, user))
                conn.commit()
            finally:
                # The connection goes back to the shared pool - put the setting back.
                conn.rollback()
                with conn.cursor() as cur:
                    cur.execute("SET lock_timeout = 0")
                conn.commit()
        if open_jobs:
            return {"status": "error", "message": "A copy is already running - press Stop first."}
    except Exception as e:
        logger.exception("Copy prod to dev could not create its job")
        return {"status": "error", "message": f"Could not start: {str(e)[:300]}"}

    # Own Lambda invocation on Lambda (a thread is frozen once the response is
    # sent); a thread when running locally.
    function_name = os.getenv("AWS_LAMBDA_FUNCTION_NAME")
    if os.getenv("LOCAL_ENV") == "1" or not function_name:
        threading.Thread(target=_run_prod_to_dev_safely, args=(job_id,), daemon=True,
                         name=f"prod-to-dev-{job_id[:8]}").start()
    else:
        try:
            from types import SimpleNamespace
            from routers.core import worker_secret
            from utils.lambda_http import LambdaRequestsManager
            LambdaRequestsManager().simulate_http(
                aws_context=SimpleNamespace(function_name=function_name),
                path=BASE_URL + "/api/prod-to-dev/run",
                method="POST",
                data={"job_id": job_id, "secret": worker_secret()},
            )
        except Exception as e:
            logger.exception("Copy prod to dev job %s could not be started", job_id)
            _job_finish(job_id, "failed", f"Could not start the copy: {str(e)[:300]}")
    return {"status": "started", "job_id": job_id}


@router.get("/settings/prod-to-dev/job/{job_id}")
def prod_to_dev_job(job_id: str, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, message, log FROM prod_to_dev_jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
        conn.rollback()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": row[0], "message": row[1] or "", "log": list(row[2] or [])}


@router.post("/settings/prod-to-dev/stop")
def prod_to_dev_stop(user: str = Depends(get_current_user)):
    """Stop every open copy job. Cancels the dev query it is running (so a
    lock wait ends now), and the copy undoes itself. A job whose worker has
    died is marked stopped after a few seconds."""
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    with get_database_connection() as conn:
        conn.rollback()
        _ensure_prod_to_dev_jobs(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE prod_to_dev_jobs SET stop_requested = TRUE "
                        "WHERE status IN ('queued', 'running') RETURNING job_id, backend_pid")
            jobs = cur.fetchall()
            for _, pid in jobs:
                if pid:
                    cur.execute("SELECT pg_cancel_backend(%s)", (pid,))
        conn.commit()
    if not jobs:
        return {"status": "ok", "message": "Nothing was running"}
    time.sleep(3)
    for job_id, _ in jobs:
        _job_finish(job_id, "stopped", "Stopped by you - nothing changed in dev")
    return {"status": "ok", "message": f"Stopped {len(jobs)} copy job(s)"}


@router.post("/api/prod-to-dev/run")
def prod_to_dev_run(data: dict):
    """Internal worker route, invoked by our own Lambda."""
    from routers.core import check_worker_secret
    check_worker_secret(data.get("secret"))
    job_id = str(data.get("job_id") or "")
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id missing")
    _run_prod_to_dev_safely(job_id)
    return {"ok": True}


def _run_prod_to_dev_safely(job_id: str) -> None:
    try:
        _run_prod_to_dev(job_id)
    except Exception as e:
        logger.exception("Copy prod to dev job %s crashed", job_id)
        try:
            _job_finish(job_id, "failed", f"Crashed: {str(e)[:300]}")
        except Exception:
            pass


def _run_prod_to_dev(job_id: str) -> None:
    """Replace the settings tables in DEV with the rows from PROD. Prod is
    only read. All-or-nothing: if any table fails, dev is left as it was."""
    import psycopg2
    import psycopg2.errors
    import psycopg2.extras
    from utils.secrets_manager_client import get_secret
    if not _job_claim(job_id):
        return

    started = time.time()

    def note(text, check_stop=True):
        stamp = f"{time.time() - started:5.1f}s  "
        lines = [stamp + t for t in text] if isinstance(text, list) else stamp + text
        try:
            _job_log(job_id, lines)
        except _CopyStopped:
            if check_stop:
                raise

    copied, skipped = [], []
    prod = None
    try:
        note("Reading prod database secret")
        data = get_secret('prod/aramis_group/data_platform/rds/aurora_pg_core_cluster_admin_credentials')
        note(f"Connecting to prod ({data['endpoint']})")
        prod = psycopg2.connect(
            host=data['endpoint'], user=data['username'], password=data['password'],
            database='carsupermarket_pricer', port=int(data.get('port', 5432)),
            connect_timeout=5,
        )
        prod.set_session(readonly=True)
        note("Connected to prod (read only)")
        with get_database_connection() as dev, prod.cursor() as pcur, dev.cursor() as dcur:
            dev.rollback()
            dcur.execute("SELECT pg_backend_pid()")
            own_pid = dcur.fetchone()[0]
            dev.commit()
            with get_database_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE prod_to_dev_jobs SET backend_pid = %s WHERE job_id = %s",
                                (own_pid, job_id))
                conn.commit()
            note(f"Connected to dev (session {own_pid})")
            try:
                # Never wait forever on a lock held by another open dev transaction.
                dcur.execute("SET LOCAL lock_timeout = '10s'")
                dcur.execute("SET LOCAL statement_timeout = '60s'")
                plan = []
                for table in PROD_TO_DEV_TABLES:
                    prod_cols = _table_columns(pcur, table)
                    dev_cols = _table_columns(dcur, table)
                    if not prod_cols or not dev_cols:
                        where = 'prod' if not prod_cols else 'dev'
                        skipped.append(f"{table} (missing in {where})")
                        note(f"Skipping {table} - table missing in {where}")
                        continue
                    cols = [c for c in prod_cols if c in dev_cols]
                    only_prod = [c for c in prod_cols if c not in dev_cols]
                    only_dev = [c for c in dev_cols if c not in prod_cols]
                    if only_prod:
                        note(f"{table}: prod-only columns not copied: {', '.join(only_prod)}")
                    if only_dev:
                        note(f"{table}: dev-only columns left empty: {', '.join(only_dev)}")
                    plan.append((table, cols))
                note(f"Checked columns - {len(plan)} tables to copy")

                # Clear children first, then load parents first.
                for table, _ in reversed(plan):
                    note(f"Clearing dev {table}")
                    try:
                        dcur.execute(f'DELETE FROM "{table}"')
                    except psycopg2.errors.LockNotAvailable:
                        dev.rollback()
                        note(f"{table} is locked by another open dev session - waited 10s and gave up")
                        holders = _lock_holders(table, own_pid)
                        for h in holders:
                            note(f"  Holding the lock: {h}")
                        if not holders:
                            note("  The other session finished before it could be looked up")
                        raise RuntimeError(f"{table} is locked by another open dev session (see log)")
                    note(f"Cleared dev {table} ({dcur.rowcount} rows removed)")
                for table, cols in plan:
                    col_sql = ', '.join(f'"{c}"' for c in cols)
                    pcur.execute(f'SELECT {col_sql} FROM "{table}"')
                    fetched = pcur.fetchall()
                    # Name each row by its primary key - values are never logged
                    # (settings holds the CAP override password).
                    keys = [c for c in _table_key_columns(pcur, table) if c in cols] or cols[:1]
                    key_idx = [cols.index(c) for c in keys]
                    # Tables keyed on more than one column (chart_targets is one
                    # row per metric per day) get one line per first-key value
                    # with a count and range, not one line per row. All lines
                    # for a table are saved in one go.
                    if len(key_idx) > 1:
                        groups = {}
                        for row in fetched:
                            rest = ' / '.join(str(row[i]) for i in key_idx[1:])
                            g = groups.setdefault(str(row[key_idx[0]]), [0, rest, rest])
                            g[0] += 1
                            g[1] = min(g[1], rest)
                            g[2] = max(g[2], rest)
                        lines = [f"  {table}: {k} - {n} rows ({lo} to {hi})" if n > 1
                                 else f"  {table}: {k} / {lo}"
                                 for k, (n, lo, hi) in groups.items()]
                    else:
                        lines = [f"  {table}: {row[key_idx[0]]}" for row in fetched]
                    if lines:
                        note(lines)
                    rows = [
                        tuple(psycopg2.extras.Json(v) if isinstance(v, (dict, list)) else v for v in row)
                        for row in fetched
                    ]
                    if rows:
                        psycopg2.extras.execute_values(
                            dcur, f'INSERT INTO "{table}" ({col_sql}) VALUES %s', rows)
                    # Move any id sequence past the copied ids so new rows do not clash.
                    if 'id' in cols:
                        dcur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (table,))
                        seq = dcur.fetchone()[0]
                        if seq:
                            dcur.execute(
                                f'SELECT setval(%s, COALESCE((SELECT MAX(id) FROM "{table}"), 0) + 1, false)',
                                (seq,))
                    copied.append(f"{table} ({len(rows)})")
                    note(f"Copied {table} - {len(rows)} rows")
                dev.commit()
                note("Saved to dev", check_stop=False)
            except BaseException:
                dev.rollback()
                note("Undone - dev left as it was", check_stop=False)
                raise
    except (_CopyStopped, psycopg2.errors.QueryCanceled) as e:
        stopped = isinstance(e, _CopyStopped) or _job_stop_requested(job_id)
        reason = "Stopped by you" if stopped else "A query ran over 60s and was cancelled"
        note(f"{reason} - nothing changed in dev", check_stop=False)
        _job_finish(job_id, "stopped" if stopped else "failed", f"{reason} - nothing changed in dev")
        return
    except Exception as e:
        logger.exception("Copy prod to dev failed")
        note(f"Failed: {str(e)[:300]}", check_stop=False)
        _job_finish(job_id, "failed", f"Nothing changed in dev: {str(e)[:300]}")
        return
    finally:
        if prod is not None:
            prod.close()
    logger.info("Copy prod to dev job %s: %s", job_id, ', '.join(copied))
    message = f"Copied {len(copied)} tables in {time.time() - started:.1f}s"
    if skipped:
        message += f", skipped {len(skipped)}"
    _job_finish(job_id, "done", message)


@router.get("/settings/users", response_class=HTMLResponse)
def users_page(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    sessions = sorted(_active_sessions.values(), key=lambda s: s['login_time'], reverse=True)
    non_admin_users = sorted(u for u in VALID_USERS if u != 'tdrayson')
    last_logins = {}
    db_active_sessions = []
    user_details = {}
    login_mode = 'both'
    try:
        with get_database_connection() as conn:
            last_logins = get_last_login_per_user(conn)
            db_active_sessions = get_active_sessions_all_envs(conn)
            user_details = load_user_details(conn)
            login_mode = get_settings(conn).get('login_mode', 'both')
    except Exception as e:
        logger.warning("Failed to load user activity data: %s", e)
    return templates.TemplateResponse("users.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "login_mode": login_mode,
        "active_sessions": sessions, "non_admin_users": non_admin_users,
        "all_users": sorted(VALID_USERS), "user_details": user_details,
        "last_logins": last_logins, "db_active_sessions": db_active_sessions,
        "sections": SETTINGS_SECTIONS, "active_section": "users",
    })


@router.post("/settings/login-mode")
def save_login_mode(
    request: Request,
    login_mode: str = Form(default="both"),
    user: str = Depends(get_current_user),
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    with get_database_connection() as conn:
        save_settings_partial(conn, {'login_mode': 'email' if login_mode == 'email' else 'both'})
    logger.info("Settings [Sign In] login_mode=%s set by %s", login_mode, user)
    return _user_redirect("users", loginok=1)


@router.post("/settings/force-relogin")
def force_relogin(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    # Rebind on the core module so get_current_user (which reads core's global) sees it
    core._sessions_invalidated_at = time.time()
    _active_sessions.clear()
    return RedirectResponse(url=BASE_URL + "/settings-page?relogin=1", status_code=303)


_REDIRECT_TARGETS = {
    "users":       "/settings/users",
    "page_access": "/settings/page-access",
    "page_names":  "/settings/page-names",
}

_PAGE_NAME_MAX = 100


@router.get("/settings/page-names", response_class=HTMLResponse)
def page_names_page(request: Request, user: str = Depends(get_current_user)):
    """One box per page, holding its current name. Blank = the default name."""
    if user != ADMIN_USERNAME:
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    try:
        with get_prod_pricer_connection() as conn:
            stored = load_page_names(conn)
        load_error = None
    except Exception as e:
        logger.warning("Page names: could not load: %s", e)
        stored, load_error = {}, str(getattr(e, "detail", None) or e)
    return templates.TemplateResponse("page_names.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "pages": PAGES, "stored": stored, "load_error": load_error,
        "max_length": _PAGE_NAME_MAX,
        "site_name_key": core.SITE_NAME_KEY, "site_name_default": core.SITE_NAME_DEFAULT,
        "sections": SETTINGS_SECTIONS, "active_section": "page_names",
    })


@router.post("/settings/page-names")
async def save_page_names_route(request: Request, user: str = Depends(get_current_user)):
    """Fields arrive as name__<page_key>. A blank box, or one holding the
    default name, stores nothing, so that page follows its default."""
    if user != ADMIN_USERNAME:
        raise HTTPException(status_code=403)
    form = await request.form()
    names = {}
    for page in PAGES:
        label = str(form.get(f"name__{page.key}", "")).strip()
        if len(label) > _PAGE_NAME_MAX:
            return _user_redirect("page_names", nameerr=f"Names+must+be+{_PAGE_NAME_MAX}+characters+or+fewer")
        if label and label != page.label:
            names[page.key] = label
    site = str(form.get("site_name", "")).strip()
    if len(site) > _PAGE_NAME_MAX:
        return _user_redirect("page_names", nameerr=f"Names+must+be+{_PAGE_NAME_MAX}+characters+or+fewer")
    if site and site != core.SITE_NAME_DEFAULT:
        names[core.SITE_NAME_KEY] = site
    try:
        with get_prod_pricer_connection() as conn:
            save_page_names(conn, names, updated_by=user)
    except Exception as e:
        logger.error("Page names save failed: %s", e)
        return _user_redirect("page_names", nameerr="Save+failed+-+see+the+log")
    # This container shows the new names straight away; others within a minute.
    core.reload_page_names()
    logger.info("Page names saved by %s: %s", user, names or 'all default')
    return _user_redirect("page_names", nameok=1)


def _user_redirect(redirect_to: str, **params):
    base = BASE_URL + _REDIRECT_TARGETS.get(redirect_to, "/settings-page")
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(url=f"{base}?{qs}", status_code=303)


@router.get("/settings/page-access", response_class=HTMLResponse)
def page_access_page(request: Request, user: str = Depends(get_current_user)):
    """Grid of users against pages. Reads only — nothing is written until a row
    is saved, so the table need not exist yet."""
    if user != ADMIN_USERNAME:
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)

    try:
        with get_database_connection() as conn:
            stored = load_all_page_access(conn)
    except Exception as e:
        logger.warning("Page access: could not load grants: %s", e)
        stored = {}

    # What each box shows. A user who has never been saved (no marker) is shown
    # the defaults, so the grid opens displaying today's real access and the
    # first save simply records it. Once saved, exactly what was stored.
    users = sorted(u for u in VALID_USERS if u != ADMIN_USERNAME)
    access = {}
    for u in users:
        rows = stored.get(u)
        if rows is None or CONFIGURED_MARKER not in rows:
            access[u] = set(DEFAULT_PAGE_KEYS)
        else:
            access[u] = rows & ALL_PAGE_KEYS

    return templates.TemplateResponse("page_access.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "pages": PAGES, "users": users, "access": access,
        "admin_user": ADMIN_USERNAME,
        "sections": SETTINGS_SECTIONS, "active_section": "page_access",
    })


@router.post("/settings/page-access")
def save_page_access(
    request: Request,
    users: list[str] = Form(default=[]),
    access: list[str] = Form(default=[]),
    user: str = Depends(get_current_user)
):
    """Save the whole grid in one go. `users` lists every row on the page (so a
    row with every box unticked is still saved); `access` holds one
    "username:page_key" value per ticked box."""
    if user != ADMIN_USERNAME:
        raise HTTPException(status_code=403)
    if ADMIN_USERNAME in users:
        raise HTTPException(status_code=400, detail="Cannot change admin access")
    missing = [u for u in users if u not in VALID_USERS]
    if missing:
        return _user_redirect("page_access", accesserr="User+not+found")

    submitted = {u: set() for u in users}
    for item in access:
        u, _, key = item.rpartition(':')
        if u not in submitted:
            return _user_redirect("page_access", accesserr="User+not+found")
        submitted[u].add(key)

    # Anything not a real page is rejected rather than ignored — this is also
    # what stops CONFIGURED_MARKER being submitted as if it were a page.
    for u, keys in submitted.items():
        unknown = keys - ALL_PAGE_KEYS
        if unknown:
            logger.warning("Page access: rejected unknown keys %s for %s", sorted(unknown), u)
            return _user_redirect("page_access", accesserr="Unknown+page")

    with get_database_connection() as conn:
        stored = load_all_page_access(conn)
        # Only write rows that changed, so granted_at/granted_by keep showing
        # when each person's access was last really changed. A user never
        # saved before counts as having the defaults, the same as the grid shows.
        changed = {}
        for u, keys in submitted.items():
            rows = stored.get(u)
            if rows is None or CONFIGURED_MARKER not in rows:
                current = set(DEFAULT_PAGE_KEYS)
            else:
                current = rows & ALL_PAGE_KEYS
            if keys != current:
                changed[u] = keys
        set_page_access_many(conn, changed, granted_by=user)

    # Update this process's cache so the grid reflects the save immediately.
    # Other containers pick it up on their own TTL — see core.PERMISSION_TTL_SECONDS.
    for u, keys in changed.items():
        core.USER_PAGE_ACCESS[u] = keys | {CONFIGURED_MARKER}
        logger.info("Page access for %s set to %s by %s", u, sorted(keys) or 'none', user)

    return _user_redirect("page_access", accessok=len(changed))


@router.post("/settings/user-details")
async def save_user_details(request: Request, user: str = Depends(get_current_user)):
    """Save first name, last name and email for every user on the page at once.
    Fields arrive as first_name__<username>, last_name__<username>, email__<username>."""
    if user != ADMIN_USERNAME:
        raise HTTPException(status_code=403)
    form = await request.form()
    details = {}
    for u in VALID_USERS:
        if f"email__{u}" not in form or u in ADMIN_DETAILS:   # admin's are fixed in code
            continue
        details[u] = {
            'first_name': str(form.get(f"first_name__{u}", "")).strip(),
            'last_name':  str(form.get(f"last_name__{u}", "")).strip(),
            'email':      str(form.get(f"email__{u}", "")).strip(),
        }
    with get_database_connection() as conn:
        update_user_details(conn, details)
    logger.info("User details saved for %d users by %s", len(details), user)
    return _user_redirect("users", detailsok=1)


@router.post("/settings/delete-user")
def delete_user(
    request: Request,
    target_user: str = Form(...),
    redirect_to: str = Form("settings"),
    user: str = Depends(get_current_user)
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    if target_user == 'tdrayson':
        raise HTTPException(status_code=400, detail="Cannot delete admin")
    if target_user not in VALID_USERS:
        return _user_redirect(redirect_to, pwderr="User+not+found")
    # One transaction for the account and its page access — a separate clear
    # could fail after the account was gone, leaving grants for a recreated
    # username to inherit.
    with get_database_connection() as conn:
        delete_user_and_access(conn, target_user)
    del VALID_USERS[target_user]
    core.USER_PAGE_ACCESS.pop(target_user, None)
    logger.info("User %s deleted by admin", target_user)
    return _user_redirect(redirect_to, userdeleted=target_user)


@router.post("/settings/add-user")
def add_user(
    request: Request,
    new_username: str = Form(...),
    new_password: str = Form(...),
    new_first_name: str = Form(default=""),
    new_last_name: str = Form(default=""),
    new_email: str = Form(default=""),
    redirect_to: str = Form("settings"),
    user: str = Depends(get_current_user)
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    username = new_username.strip().lower()
    if not username:
        return _user_redirect(redirect_to, pwderr="Username+cannot+be+empty")
    if username in VALID_USERS:
        return _user_redirect(redirect_to, pwderr="User+already+exists")
    if not new_password or len(new_password) < 6:
        return _user_redirect(redirect_to, pwderr="Password+must+be+at+least+6+characters")
    pwd_hash = hashlib.sha256(new_password.encode()).hexdigest()
    VALID_USERS[username] = pwd_hash
    with get_database_connection() as conn:
        upsert_user(conn, username, pwd_hash)
        update_user_details(conn, {username: {
            'first_name': new_first_name.strip(),
            'last_name':  new_last_name.strip(),
            'email':      new_email.strip(),
        }})
    logger.info("User %s created by admin", username)
    return _user_redirect(redirect_to, useradded=username)


@router.post("/settings/set-password")
def set_password(
    request: Request,
    target_user: str = Form(...),
    new_password: str = Form(...),
    redirect_to: str = Form("settings"),
    user: str = Depends(get_current_user)
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    if target_user == 'tdrayson':
        raise HTTPException(status_code=400, detail="Cannot change admin password here")
    if target_user not in VALID_USERS:
        raise HTTPException(status_code=404, detail="User not found")
    if not new_password or len(new_password) < 6:
        return _user_redirect(redirect_to, pwderr="Password+must+be+at+least+6+characters")
    pwd_hash = hashlib.sha256(new_password.encode()).hexdigest()
    VALID_USERS[target_user] = pwd_hash
    with get_database_connection() as conn:
        upsert_user(conn, target_user, pwd_hash)
    logger.info("Password updated for %s by admin", target_user)
    return _user_redirect(redirect_to, pwdok=target_user)


@router.post("/settings")
def update_settings(
    request: Request,
    number_to_buy: int = Form(...),
    max_days_sales: int = Form(...),
    max_dts: int = Form(...),
    min_gpu_m: int = Form(...),
    cap_workers: int = Form(...),
    cap_rate_limit: int = Form(...),
    calc_buying_reqs: str = Form(default=""),
    get_stock: str = Form(default=""),
    enable_cars_bought: str = Form(default=""),
    kpi_ai_summary: str = Form(default=""),
    mot_workers: int = Form(default=5),
    at_workers: int = Form(default=40),
    at_rate_limit: int = Form(default=60),
    valuation_cache_hours: int = Form(default=36),
    cap_override_enabled: str = Form(default=""),
    cap_override_subscriber_id: str = Form(default=""),
    cap_override_password: str = Form(default=""),
    user: str = Depends(get_current_user),
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    calc = calc_buying_reqs == "on"
    gs = get_stock == "on"
    ecb = enable_cars_bought == "on"
    kai = kpi_ai_summary == "on"
    coe = cap_override_enabled == "on"
    with get_database_connection() as conn:
        save_settings(conn, number_to_buy, max_days_sales, max_dts, min_gpu_m, cap_workers, cap_rate_limit, calc, gs, enable_cars_bought=ecb, kpi_ai_summary=kai, mot_workers=mot_workers, at_workers=at_workers, at_rate_limit=at_rate_limit, valuation_cache_hours=valuation_cache_hours, cap_override_enabled=coe, cap_override_subscriber_id=cap_override_subscriber_id.strip(), cap_override_password=cap_override_password.strip(), user='buyer')
    # Reset CAPClient singleton so the next task picks up the new creds
    from list_processor.cap_client import CAPClient
    from utils.singleton import Singleton
    Singleton._instances.pop(CAPClient, None)
    logger.info(
        "Settings updated by %s: number_to_buy=%d, max_days_sales=%d, max_dts=%d, "
        "min_gpu_m=%d, cap_workers=%d, cap_rate_limit=%d, at_workers=%d, at_rate_limit=%d, calc_buying_reqs=%s, get_stock=%s, enable_cars_bought=%s, mot_workers=%d",
        user, number_to_buy, max_days_sales, max_dts, min_gpu_m, cap_workers, cap_rate_limit, at_workers, at_rate_limit, calc, gs, ecb, mot_workers
    )
    return RedirectResponse(url=BASE_URL + "/settings-page?saved=1", status_code=303)
