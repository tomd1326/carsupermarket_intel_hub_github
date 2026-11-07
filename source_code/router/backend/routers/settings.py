"""Settings page, user management and app settings (admin only)."""

from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
import csv
import datetime
import io
import time
import urllib.parse
import logging
import os
import re
import subprocess
import zoneinfo


from utils.db_utils import (get_database_connection, get_settings, save_settings, SETTINGS_DEFAULTS,
                            save_settings_partial, get_last_login_per_user,
                            get_active_sessions, get_sessions, get_session_environments,
                            get_session_usernames, get_prod_activity_connection,
                            user_sessions_ready, read_session_settings, read_user_details,
                            close_sessions, SESSION_LIST_LIMIT,
                            load_user_details, update_user_details, read_role_users, set_user_role,
                            USER_PERMISSIONS, read_user_permissions, set_user_permissions,
                            load_all_page_access, set_page_access_many,
                            delete_user_and_access, ALWAYS_ENABLED_USERS,
                            APPEARANCE_COLOUR_SETTINGS,
                            get_prod_pricer_connection, load_page_names, save_page_names,
                            add_user,
                            ensure_user_nexus_columns, read_user_nexus_details, add_nexus_users,
                            update_user_org, fill_user_nexus_details, email_match_key,
                            NEXUS_USER_COLUMNS)
from utils import nexus_ingest
from utils.ddl_once import once_per_database

from . import core
from .core import (BASE_URL, templates, VALID_USERS, get_current_user, session_config,
                   ADMIN_USERNAME, ADMIN_USERNAMES, is_admin, _uk_now)
from .page_registry import PAGES, ALL_PAGE_KEYS, CONFIGURED_MARKER, shown_page_access

logger = logging.getLogger(__name__)

router = APIRouter()


# Settings sections. Each renders its own page with its own Save button, and
# each save writes only that section's keys (see save_settings_partial) so one
# page cannot overwrite fields it never displayed.
# Settings for one page, listed under Page Settings in the nav.
# slug -> page key; the label and heading are the page's name from Page Names.
PAGE_SETTINGS_SECTIONS = [
    ("buying-requirements", "buying_requirements"),
    ("supplier-evaluator",  "tasks"),
    ("mot-check",           "mot_check"),
    ("email-attachments",   "email_attachments"),
]
templates.env.globals['page_settings_sections'] = PAGE_SETTINGS_SECTIONS

# slug -> (nav label, page heading)
SETTINGS_SECTIONS = [
    ("ai",                 "AI",                  "AI"),
    ("cap-credentials",     "CAP Credentials",     "CAP Credential Override"),
    ("email",               "Email",               "Email"),
    ("email-alerts",        "Email Alerts",        "Email Alerts"),
    ("live-users",          "Live Users",          "Live Users"),
    ("appearance",          "Appearance",          "Appearance"),
    ("sharepoint",          "SharePoint",          "SharePoint Files"),
    ("ga4-requests",        "GA4 Requests",        "GA4 Requests"),
    ("external-data",       "External Data",       "External Data"),
    ("prod-to-dev",         "Prod To Dev",         "Copy Prod To Dev"),
    ("live-update",         "Live Update",         "Live Update"),
    ("google",              "Google",              "Google Slides"),
]

# Sections merged into another; old links land on the new one.
_MOVED_SECTIONS = {"api-requests": "live-update", "dbt": "live-update", "kpi": "ai"}

# The settings 'user' scope used by this admin page. The buying requirements
# page reads its own per-user values, but the admin page has always edited the
# shared 'buyer' scope — keep that unchanged.
_SETTINGS_SCOPE = 'buyer'


def _section_title(slug: str) -> str | None:
    """The section's heading, or None if there is no such section."""
    page_key = dict(PAGE_SETTINGS_SECTIONS).get(slug)
    if page_key:
        return core.page_name(page_key)
    return next((s[2] for s in SETTINGS_SECTIONS if s[0] == slug), None)


def _render_section(request: Request, user: str, slug: str):
    section_title = _section_title(slug)
    if section_title is None:
        return RedirectResponse(url=BASE_URL + "/settings-page", status_code=303)
    with get_database_connection() as conn:
        settings = get_settings(conn, user=_SETTINGS_SCOPE)
    google = None
    if slug == "google":
        from utils import google_oauth
        google = google_oauth.status()
        google["message"] = request.session.pop("google_message", "")
    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "settings": settings,
        "sections": SETTINGS_SECTIONS,
        "active_section": slug,
        "section_title": section_title,
        "google": google,
    })


@router.get("/settings-page", response_class=HTMLResponse)
def settings_page(request: Request, user: str = Depends(get_current_user)):
    """Landing on the settings area — show the first section."""
    if not is_admin(user):
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    return _render_section(request, user, PAGE_SETTINGS_SECTIONS[0][0])


@router.get("/settings/section/{slug}", response_class=HTMLResponse)
def settings_section(slug: str, request: Request, user: str = Depends(get_current_user)):
    if not is_admin(user):
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    if slug in _MOVED_SECTIONS:
        return RedirectResponse(url=f"{BASE_URL}/settings/section/{_MOVED_SECTIONS[slug]}", status_code=303)
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
    if not is_admin(user):
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
    if not is_admin(user):
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
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    with get_database_connection() as conn:
        save_settings_partial(conn, {'mot_workers': mot_workers})
    logger.info("Settings [MOT Check] updated by %s", user)
    return _section_redirect("mot-check")


@router.post("/settings/section/email-attachments")
def save_email_attachments(
    request: Request,
    email_attach_skip_inline: str = Form(default=""),
    email_attach_inline_keep_kb: int = Form(...),
    user: str = Depends(get_current_user),
):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    with get_database_connection() as conn:
        save_settings_partial(conn, {
            'email_attach_skip_inline':    1 if email_attach_skip_inline == "on" else 0,
            'email_attach_inline_keep_kb': max(0, email_attach_inline_keep_kb),
        })
    logger.info("Settings [Email Attachments] updated by %s", user)
    return _section_redirect("email-attachments")


@router.post("/settings/ai/refresh-models")
def refresh_ai_models(request: Request, user: str = Depends(get_current_user)):
    """Used to ask Snowflake which Cortex models it serves. The website no
    longer uses Snowflake Cortex (see utils/cortex.py), so it only says so."""
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    from utils.cortex import AI_OFF_MESSAGE
    return JSONResponse({"error": AI_OFF_MESSAGE}, status_code=400)


@router.post("/settings/section/ai")
def save_ai(
    request: Request,
    kpi_ai_summary: str = Form(default=""),
    orders_deliveries_ai: str = Form(default=""),
    cortex_model: str = Form(...),
    user: str = Depends(get_current_user),
):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    with get_database_connection() as conn:
        save_settings_partial(conn, {
            'kpi_ai_summary':       '1' if kpi_ai_summary == "on" else '0',
            'orders_deliveries_ai': '1' if orders_deliveries_ai == "on" else '0',
            'cortex_model':         cortex_model.strip() or SETTINGS_DEFAULTS['cortex_model'],
        })
    logger.info("Settings [AI] updated by %s", user)
    return _section_redirect("ai")


@router.post("/settings/section/cap-credentials")
def save_cap_credentials(
    request: Request,
    cap_override_enabled: str = Form(default=""),
    cap_override_subscriber_id: str = Form(default=""),
    cap_override_password: str = Form(default=""),
    user: str = Depends(get_current_user),
):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    with get_database_connection() as conn:
        save_settings_partial(conn, {
            'cap_override_enabled':       '1' if cap_override_enabled == "on" else '0',
            'cap_override_subscriber_id': cap_override_subscriber_id.strip(),
            'cap_override_password':      cap_override_password.strip(),
        })
    # Reset CAPClient singleton so the next task picks up the new creds
    from clients.cap_client import CAPClient
    from utils.singleton import Singleton
    Singleton._instances.pop(CAPClient, None)
    logger.info("Settings [CAP Credentials] updated by %s", user)
    return _section_redirect("cap-credentials")


# ── Google (Weekly Business Review Slides export) ─────────────────────────────
# See utils/google_oauth.py. Only the admin can connect or disconnect.

@router.get("/settings/google/connect")
def google_connect(request: Request, user: str = Depends(get_current_user)):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    import secrets
    from utils import google_oauth
    state = secrets.token_urlsafe(32)
    request.session["google_oauth_state"] = state
    try:
        return RedirectResponse(url=google_oauth.auth_url(request, state), status_code=303)
    except google_oauth.GoogleNotConnected as e:
        request.session["google_message"] = str(e)
        return RedirectResponse(url=f"{BASE_URL}/settings/section/google", status_code=303)


@router.get("/settings/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = "",
                    user: str = Depends(get_current_user)):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    from utils import google_oauth
    expected = request.session.pop("google_oauth_state", None)
    if error:
        message = f"Google login cancelled ({error})."
    elif not expected or state != expected:
        message = "Google login rejected: the request did not come from this page. Try Connect again."
    else:
        try:
            email = google_oauth.connect(request, code)
            message = f"Connected as {email}." if email else "Connected."
            logger.info("Google connected by %s", user)
        except google_oauth.GoogleNotConnected as e:
            message = str(e)
        except Exception as e:
            logger.error(f"Google connect failed: {e}")
            message = "Google connect failed - see the server log."
    request.session["google_message"] = message
    return RedirectResponse(url=f"{BASE_URL}/settings/section/google", status_code=303)


@router.post("/settings/google/disconnect")
def google_disconnect(request: Request, user: str = Depends(get_current_user)):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    from utils import google_oauth
    google_oauth.disconnect()
    logger.info("Google disconnected by %s", user)
    request.session["google_message"] = "Disconnected."
    return RedirectResponse(url=f"{BASE_URL}/settings/section/google", status_code=303)


@router.post("/settings/section/email")
def save_email(
    request: Request,
    email_sender: str = Form(default=""),
    user: str = Depends(get_current_user),
):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    with get_database_connection() as conn:
        save_settings_partial(conn, {'email_sender': email_sender.strip()})
    logger.info("Settings [Email] updated by %s", user)
    return _section_redirect("email")


@router.post("/settings/section/live-users")
def save_live_users(
    request: Request,
    presence_enabled: str = Form(default=""),
    presence_users_see_each_other: str = Form(default=""),
    presence_users_see_home_list: str = Form(default=""),
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
    if not is_admin(user):
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
            'presence_users_see_each_other': '1' if presence_users_see_each_other == "on" else '0',
            'presence_users_see_home_list':  '1' if presence_users_see_home_list == "on" else '0',
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


# Settings > Appearance: every colour, opacity and size the pages share.
# core.appearance_css turns these into the tokens in theme.css.
_APPEARANCE_INTS = {
    # key: (lowest, highest)
    'account_menu_font_size': (10, 24), 'side_menu_font_size': (10, 24), 'settings_font_size': (10, 20), 'side_menu_width': (180, 480),
    'page_title_size': (14, 40), 'page_menu_btn_size': (24, 64), 'page_head_pad_y': (0, 40),
    'page_head_pad_x': (0, 60), 'page_head_gap': (0, 40),
    'ap_surface_opacity': (0, 100), 'ap_header_opacity': (0, 100), 'ap_menu_opacity': (0, 100),
    'ap_radius': (0, 30), 'ap_header_shadow': (0, 100),
    'home_title_size': (14, 80), 'home_title_gap': (0, 120), 'home_logo_size': (20, 240),
    'home_logo_radius': (0, 120), 'home_logo_gap': (0, 80), 'home_btn_width': (100, 600),
    'home_btn_height': (30, 200), 'home_btn_font_size': (10, 40), 'home_btn_font_weight': (400, 700),
    'home_btn_radius': (0, 100), 'home_btn_3d': (0, 100), 'home_btn_3d_edge': (0, 80), 'home_grid_gap': (0, 80), 'home_grid_max_width': (200, 1600),
    'home_pad_top': (0, 600), 'home_pad_side': (0, 100), 'home_pad_bottom': (0, 30),
}


@router.post("/settings/section/appearance")
async def save_appearance(request: Request, user: str = Depends(get_current_user)):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    form = await request.form()
    values = {}
    for key in APPEARANCE_COLOUR_SETTINGS:
        # Colours go into a style block, so only '#rrggbb' is accepted.
        value = str(form.get(key, "")).strip().lower()
        if not re.fullmatch(r'#[0-9a-f]{6}', value):
            raise HTTPException(status_code=400, detail=f"{key} must be a colour like #ffffff")
        values[key] = value
    for key, (low, high) in _APPEARANCE_INTS.items():
        try:
            values[key] = min(high, max(low, int(str(form.get(key, "")).strip())))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{key} must be a whole number")
    with get_database_connection() as conn:
        save_settings_partial(conn, values)
    # This container uses the new values straight away; others within a minute.
    core.reload_presence_config()
    logger.info("Settings [Appearance] updated by %s", user)
    return _section_redirect("appearance")


@router.post("/settings/section/appearance/reset")
async def reset_appearance(user: str = Depends(get_current_user)):
    """Every Appearance setting back to its default (db_utils.SETTINGS_DEFAULTS)."""
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    keys = set(APPEARANCE_COLOUR_SETTINGS) | set(_APPEARANCE_INTS)
    with get_database_connection() as conn:
        save_settings_partial(conn, {k: SETTINGS_DEFAULTS[k] for k in keys})
    core.reload_presence_config()
    logger.info("Settings [Appearance] reset to defaults by %s", user)
    return _section_redirect("appearance")

# Sign in to AWS again when the SSO session has expired. Only works where the
# app runs on your own machine (not on Lambda): `aws sso login` opens the sign-in
# page in a browser on the machine it runs on, so on Lambda it would do nothing
# useful. Started from Sign In To AWS in the account menu (aws_login.js), which
# only shows when the app runs locally.
# Errors are returned as JSON, not raised: a raised HTTPException is turned into
# the HTML error page (exceptions.py), which the button's script cannot read.
_sso_login_process = None


@router.post("/api/aws-sso-login")
def aws_sso_login(user: str = Depends(get_current_user)):
    global _sso_login_process
    if not user:
        raise HTTPException(status_code=401)
    # Lambda always sets AWS_LAMBDA_FUNCTION_NAME; anywhere else is your own computer.
    if os.getenv('AWS_LAMBDA_FUNCTION_NAME'):
        return JSONResponse({"detail": "AWS sign-in can only be started when the app runs on your own computer"}, status_code=400)
    if _sso_login_process is not None and _sso_login_process.poll() is None:
        return {"status": "running"}
    command = ["aws", "sso", "login"]
    profile = os.getenv("AWS_PROFILE")
    if profile:
        command += ["--profile", profile]
    try:
        _sso_login_process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        logger.error("aws sso login could not start: %s", e)
        return JSONResponse({"detail": f"Could not start the AWS CLI: {e}"}, status_code=500)
    logger.info("aws sso login started for %s", user)
    return {"status": "running"}


@router.get("/api/aws-sso-login")
def aws_sso_login_status(user: str = Depends(get_current_user)):
    """running while the sign-in is open, then ok or failed. Local only, like the POST."""
    if not user:
        raise HTTPException(status_code=401)
    proc = _sso_login_process
    if proc is None:
        return {"status": "idle"}
    code = proc.poll()
    if code is None:
        return {"status": "running"}
    return {"status": "ok" if code == 0 else "failed"}

@router.post("/settings/live-users/test-photo")
def test_microsoft_photo(email: str = Form(...), user: str = Depends(get_current_user)):
    """Fetch one person's Microsoft 365 photo through Graph, to check the app
    registration has a photo permission before building on it. Also reports
    which application permissions the token carries."""
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    import base64
    import json as _json
    import requests
    from utils.email_client import _get_token
    try:
        token = _get_token(use_mailer=False)
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
    """Send a test email from the saved sender to tom.drayson only, to confirm
    Graph mail sending works. Uses the SAVED sender, not the box."""
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    from utils.email_client import send_email, configured_sender
    sender = configured_sender()
    to = "tom.drayson@carsupermarket.com"
    sent_at = datetime.datetime.now(zoneinfo.ZoneInfo("Europe/London")).strftime('%Y-%m-%d %H:%M:%S')
    try:
        send_email(
            to=to,
            subject=f"Intelligence Hub test email - {sent_at}",
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
    if not is_admin(user):
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
    'ai_knowledge',
    'ai_page_settings',
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
@once_per_database
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
    if not is_admin(user):
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
    if not is_admin(user):
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
    if not is_admin(user):
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


def _users_source(q) -> tuple:
    """(can_view_prod, source) for the Users page and its CSV export. Local
    and dev open on prod's accounts (the real users) unless ?source=this;
    prod itself only has its own."""
    can_view_prod = core._ENV != 'prod'
    source = 'this' if not can_view_prod or q.get('source') == 'this' else 'prod'
    return can_view_prod, source


def _read_prod_users() -> tuple:
    """(people, last_logins, buyers, stored_access, permissions) from
    prod's database. Read only: nothing is created or changed there. buyers is
    the usernames with the Buyer role."""
    with get_prod_activity_connection() as conn:
        try:
            people = read_user_details(conn)
            last_logins = get_last_login_per_user(conn, write=False)
            buyers = read_role_users(conn, 'buyer', ensure=False)
            for username, extra in read_user_nexus_details(conn).items():
                if username in people:
                    people[username].update(extra)
            stored_access = load_all_page_access(conn)
            permissions = read_user_permissions(conn, ensure=False)
        except Exception:
            conn.rollback()
            raise
    return people, last_logins, buyers, stored_access, permissions


@router.get("/settings/users", response_class=HTMLResponse)
def users_page(request: Request, user: str = Depends(get_current_user)):
    if not is_admin(user):
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    can_view_prod, source = _users_source(request.query_params)
    viewing_prod = can_view_prod and source == 'prod'
    last_logins = {}
    people = {}
    all_users = []
    buyers = set()
    # None means it could not be read: the popup then hides the ticks, so a save
    # cannot overwrite access nobody was shown.
    stored_access = None
    # The same for the Permissions ticks.
    stored_permissions = None
    error = None
    if viewing_prod:
        # Never falls back to this environment's users under a Prod heading.
        try:
            people, last_logins, buyers, stored_access, stored_permissions = _read_prod_users()
            all_users = sorted(people)
        except Exception as e:
            logger.warning("Failed to load prod user accounts: %s", e)
            error = str(getattr(e, 'detail', None) or e)[:300]
    else:
        try:
            with get_database_connection() as conn:
                last_logins = get_last_login_per_user(conn)
                people = load_user_details(conn)
                buyers = read_role_users(conn, 'buyer')
                ensure_user_nexus_columns(conn)
                for username, extra in read_user_nexus_details(conn).items():
                    if username in people:
                        people[username].update(extra)
                stored_access = load_all_page_access(conn)
                stored_permissions = read_user_permissions(conn)
        except Exception as e:
            logger.warning("Failed to load user accounts data: %s", e)
        all_users = sorted(VALID_USERS)
    # Pages hidden on Settings > Page Names get no tick box and are not counted.
    pages = core.visible_pages()
    visible_keys = {p.key for p in pages}
    access = {u: shown_page_access(stored_access.get(u)) & visible_keys
              for u in all_users} if stored_access is not None else {}
    return templates.TemplateResponse("users.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "admin_username": ADMIN_USERNAME, "buyers": buyers,
        "all_users": all_users, "people": people,
        "last_logins": last_logins, "error": error,
        "pages": pages, "access": access, "access_loaded": stored_access is not None,
        "permission_list": USER_PERMISSIONS, "user_permissions": stored_permissions or {},
        "permissions_loaded": stored_permissions is not None,
        "can_view_prod": can_view_prod, "source": source, "viewing_prod": viewing_prod,
        "this_env": core._ENV,
        "sections": SETTINGS_SECTIONS, "active_section": "users",
    })


@router.get("/settings/users/export")
def users_export(request: Request, user: str = Depends(get_current_user)):
    """CSV of the accounts the Users page shows: prod (read only) or this
    environment, chosen by ?source=. No passwords or hashes."""
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    can_view_prod, source = _users_source(request.query_params)
    try:
        if can_view_prod and source == 'prod':
            people, last_logins, _buyers, _access, _permissions = _read_prod_users()
            usernames = sorted(people)
        else:
            with get_database_connection() as conn:
                last_logins = get_last_login_per_user(conn)
                people = load_user_details(conn)
                for username, extra in read_user_nexus_details(conn).items():
                    if username in people:
                        people[username].update(extra)
            usernames = sorted(VALID_USERS)
    except Exception as e:
        logger.warning("Users export failed (%s): %s", source, e)
        raise HTTPException(status_code=500, detail="Could not load users for the export")

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['Username', 'First Name', 'Last Name', 'Email', 'Nexus User ID', 'Site ID', 'Site Name',
                     'Site Type', 'Company ID', 'Company Name', 'Company Type', 'Last Login', 'Last Seen'])
    for u in usernames:
        d = people.get(u, {})
        ll = last_logins.get(u, {})
        writer.writerow([_csv_cell(v) for v in (
            u, d.get('first_name') or '', d.get('last_name') or '', d.get('email') or '',
            *[d.get(c) if d.get(c) is not None else '' for c in NEXUS_USER_COLUMNS],
            ll.get('last_login') or 'Never', ll.get('last_seen') or 'Never',
        )])
    filename = f"users_{source}_{_uk_now().date().isoformat()}.csv"
    # The BOM makes Excel read the file as UTF-8.
    return Response(
        content='﻿' + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _nexus_host() -> str:
    """The API Host saved on Settings > Live Update, else the default there."""
    from .api_requests import SOURCES
    host = SOURCES['nexus']['defaults']['nexus_host']
    try:
        with get_database_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = 'nexus_host'")
            row = cur.fetchone()
            if row and str(row[0]).startswith('https://'):
                host = str(row[0])
    except Exception as e:
        logger.warning("Could not read the Nexus host setting: %s", e)
    return host.rstrip('/')


def _existing_user_keys(source: str) -> set:
    """Lower-case usernames and emails already in the chosen database."""
    with (get_prod_activity_connection() if _writes_to_prod(source) else get_database_connection()) as conn:
        people = read_user_details(conn)
    return {email_match_key(v) for u, d in people.items() for v in (u, d.get('email') or '') if v}


@router.get("/settings/users/nexus")
def nexus_users(request: Request, user: str = Depends(get_current_user)):
    """Every Nexus Point user, each marked existing when their email is already
    a username or a user's email here."""
    if not is_admin(user):
        raise HTTPException(status_code=403)
    source = request.query_params.get('source') or 'this'
    try:
        rows = nexus_ingest.fetch_users_with_units(_nexus_host())
        taken = _existing_user_keys(source)
    except Exception as e:
        logger.warning("Nexus users fetch failed: %s", e)
        return JSONResponse({"error": str(e)[:300]}, status_code=502)
    for r in rows:
        r['existing'] = email_match_key(r['email']) in taken
    rows.sort(key=lambda r: (r['existing'], (r['first_name'] + ' ' + r['last_name']).lower()))
    return JSONResponse({"rows": rows})


def _int_or_none(value):
    try:
        return int(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


@router.post("/settings/users/nexus-import")
async def nexus_users_import(request: Request, user: str = Depends(get_current_user)):
    """Create the ticked Nexus users as pending accounts (activated later with
    a link from Edit). Existing emails are skipped."""
    if not is_admin(user):
        raise HTTPException(status_code=403)
    data = await request.json()
    source = data.get('source') or 'this'
    rows = []
    for r in data.get('rows') or []:
        if not isinstance(r, dict) or '@' not in str(r.get('email') or ''):
            continue
        rows.append({
            'email': str(r['email'])[:255], 'first_name': str(r.get('first_name') or '')[:100],
            'last_name': str(r.get('last_name') or '')[:100],
            'nexus_user_id': _int_or_none(r.get('nexus_user_id')),
            'site_id': _int_or_none(r.get('site_id')), 'company_id': _int_or_none(r.get('company_id')),
            'site_name': str(r.get('site_name') or '')[:255], 'site_type': str(r.get('site_type') or '')[:100],
            'company_name': str(r.get('company_name') or '')[:255],
            'company_type': str(r.get('company_type') or '')[:100],
        })
    to_prod = _writes_to_prod(source)
    try:
        with (get_prod_activity_connection() if to_prod else get_database_connection()) as conn:
            created = add_nexus_users(conn, rows)
    except Exception as e:
        logger.warning("Nexus users import failed: %s", e)
        return JSONResponse({"error": str(e)[:300]}, status_code=500)
    if not to_prod:
        with get_database_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT username FROM users WHERE username = ANY(%s)", (created,))
            VALID_USERS.update(row[0] for row in cur.fetchall())
        core.reload_user_details()
    logger.info("%d Nexus users imported%s by %s: %s", len(created), " to PROD" if to_prod else "",
                user, ', '.join(created))
    return JSONResponse({"created": created})


@router.post("/settings/users/nexus-fill")
async def nexus_users_fill(request: Request, user: str = Depends(get_current_user)):
    """Fill in empty Nexus ids, site and company for the users already here,
    matched on email. Nothing already filled is changed."""
    if not is_admin(user):
        raise HTTPException(status_code=403)
    data = await request.json()
    source = data.get('source') or 'this'
    to_prod = _writes_to_prod(source)
    try:
        rows = nexus_ingest.fetch_users_with_units(_nexus_host())
        with (get_prod_activity_connection() if to_prod else get_database_connection()) as conn:
            updated, unmatched = fill_user_nexus_details(conn, rows)
    except Exception as e:
        logger.warning("Fill users from Nexus failed: %s", e)
        return JSONResponse({"error": str(e)[:300]}, status_code=500)
    logger.info("%d users filled from Nexus%s by %s (%d not in Nexus)", updated,
                " on PROD" if to_prod else "", user, unmatched)
    return JSONResponse({"updated": updated, "unmatched": unmatched})


_ENDED_BY_LABELS = {
    'active':           'Still Active',
    'logout':           'Logout',
    'timeout':          'Timed Out',
    'forced':           'Forced',
    'password_changed': 'Password Changed',   # sessions from before single sign-on
    'user_deleted':     'User Deleted',
}
_STARTED_BY_LABELS = {
    'sso':      'Sign In',
    'password': 'Sign In',   # sessions from before single sign-on
    'cookie':   'Carried Over',
    'returned': 'Came Back After Timeout',
}
_ACTIVITY_DEFAULT_DAYS = 30
_EXPORT_LIMIT = 100000


def _parse_date(value: str | None, fallback: datetime.date) -> datetime.date:
    try:
        return datetime.date.fromisoformat((value or '').strip())
    except ValueError:
        return fallback


def _activity_filters(q) -> dict:
    """The filters and database choice from the query string, shared by the
    User Activity page and its CSV export so both always show the same rows."""
    today = _uk_now().date()
    date_to = _parse_date(q.get('date_to'), today)
    date_from = _parse_date(q.get('date_from'), date_to - datetime.timedelta(days=_ACTIVITY_DEFAULT_DAYS))
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    f_ended = q.get('ended_by') or None
    if f_ended not in _ENDED_BY_LABELS:
        f_ended = None
    # Local and dev can show prod's sessions (the real users) or their own.
    # Prod's are read only: nothing is changed in the prod database from here.
    can_view_prod = core._ENV != 'prod'
    source = 'this' if not can_view_prod or q.get('source') == 'this' else 'prod'
    return {
        'date_from': date_from, 'date_to': date_to,
        'f_user': q.get('user') or None, 'f_env': q.get('env') or None, 'f_ended': f_ended,
        'can_view_prod': can_view_prod, 'source': source,
        'viewing_prod': can_view_prod and source == 'prod',
    }


@router.get("/settings/user-activity", response_class=HTMLResponse)
def user_activity_page(request: Request, user: str = Depends(get_current_user)):
    if not is_admin(user):
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    flt = _activity_filters(request.query_params)
    date_from, date_to = flt['date_from'], flt['date_to']
    f_user, f_env, f_ended = flt['f_user'], flt['f_env'], flt['f_ended']
    can_view_prod, source, viewing_prod = flt['can_view_prod'], flt['source'], flt['viewing_prod']
    timeout = session_config()['session_timeout_minutes']
    sessions, active, environments, usernames, people = [], [], [], [], {}
    error = None
    not_ready = False
    try:
        if viewing_prod:
            with get_prod_activity_connection() as conn:
                try:
                    if not user_sessions_ready(conn):
                        not_ready = True
                    else:
                        timeout = read_session_settings(conn)['session_timeout_minutes']
                        active = get_active_sessions(conn, timeout, write=False)
                        sessions = get_sessions(conn, timeout, date_from, date_to, f_user, f_env, f_ended,
                                                write=False)
                        environments = get_session_environments(conn)
                        usernames = get_session_usernames(conn)
                        people = read_user_details(conn)
                except Exception:
                    conn.rollback()
                    raise
        else:
            with get_database_connection() as conn:
                active = get_active_sessions(conn, timeout)
                sessions = get_sessions(conn, timeout, date_from, date_to, f_user, f_env, f_ended)
                environments = get_session_environments(conn)
                usernames = sorted(set(VALID_USERS) | set(get_session_usernames(conn)))
                people = read_user_details(conn)
    except Exception as e:
        logger.warning("Failed to load user activity (%s): %s", source, e)
        error = str(getattr(e, 'detail', None) or e)[:300]
    return templates.TemplateResponse("user_activity.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "sessions": sessions, "active_sessions": active, "error": error,
        "truncated": len(sessions) >= SESSION_LIST_LIMIT, "limit": SESSION_LIST_LIMIT,
        "usernames": usernames, "people": people, "environments": environments,
        "ended_by_labels": _ENDED_BY_LABELS, "started_by_labels": _STARTED_BY_LABELS,
        "f_user": f_user or '', "f_env": f_env or '', "f_ended": f_ended or '',
        "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
        "timeout_minutes": timeout,
        "can_view_prod": can_view_prod, "source": source, "viewing_prod": viewing_prod,
        "not_ready": not_ready, "this_env": core._ENV,
        "sections": SETTINGS_SECTIONS, "active_section": "user_activity",
    })


_CSV_FORMULA_STARTS = ('=', '+', '-', '@', '\t', '\r')


def _csv_cell(value):
    """Text starting with = + - @ is read as a formula by Excel; a leading
    apostrophe-style space keeps names and emails as plain text."""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_STARTS):
        return ' ' + value
    return value


@router.get("/settings/user-activity/export")
def user_activity_export(request: Request, user: str = Depends(get_current_user)):
    """CSV of every session the User Activity page's filters match, with no
    row limit beyond _EXPORT_LIMIT. Same database as the page: prod (read
    only) or this environment, chosen by ?source=."""
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    flt = _activity_filters(request.query_params)
    timeout = session_config()['session_timeout_minutes']
    try:
        if flt['viewing_prod']:
            with get_prod_activity_connection() as conn:
                try:
                    if not user_sessions_ready(conn):
                        raise HTTPException(status_code=409, detail="Prod has no session data yet - this change is not deployed there")
                    timeout = read_session_settings(conn)['session_timeout_minutes']
                    sessions = get_sessions(conn, timeout, flt['date_from'], flt['date_to'], flt['f_user'],
                                            flt['f_env'], flt['f_ended'], write=False, limit=_EXPORT_LIMIT)
                    people = read_user_details(conn)
                except Exception:
                    conn.rollback()
                    raise
        else:
            with get_database_connection() as conn:
                sessions = get_sessions(conn, timeout, flt['date_from'], flt['date_to'], flt['f_user'],
                                        flt['f_env'], flt['f_ended'], limit=_EXPORT_LIMIT)
                people = read_user_details(conn)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("User activity export failed (%s): %s", flt['source'], e)
        raise HTTPException(status_code=500, detail="Could not load sessions for the export")

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['User', 'Name', 'Email', 'Env', 'Started', 'Last Activity', 'Ended',
                     'Duration (Minutes)', 'Ended By', 'Started By'])
    for s in sessions:
        d = people.get(s['user'], {})
        name = f"{d.get('first_name') or ''} {d.get('last_name') or ''}".strip()
        writer.writerow([_csv_cell(v) for v in (
            s['user'], name, d.get('email') or '', s['environment'], s['started'], s['last_activity'],
            s['ended'], round(s['duration_seconds'] / 60, 1),
            _ENDED_BY_LABELS.get(s['end_reason'], s['end_reason']),
            _STARTED_BY_LABELS.get(s['started_by'], s['started_by']),
        )])
    filename = f"user_activity_{flt['source']}_{flt['date_from']}_{flt['date_to']}.csv"
    # The BOM makes Excel read the file as UTF-8.
    return Response(
        content='﻿' + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/settings/user-activity/timeout")
def save_session_timeout(
    request: Request,
    session_timeout_minutes: int = Form(...),
    user: str = Depends(get_current_user),
):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    minutes = max(1, min(24 * 60, session_timeout_minutes))
    with get_database_connection() as conn:
        save_settings_partial(conn, {'session_timeout_minutes': minutes})
    core.reload_session_config()
    logger.info("Settings [User Activity] session_timeout_minutes=%s set by %s", minutes, user)
    return _user_redirect("user_activity", source="this", timeoutok=1)


@router.post("/settings/force-relogin")
def force_relogin(request: Request, user: str = Depends(get_current_user)):
    if not is_admin(user):
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    # Stored in the settings table so every container signs people out, not
    # just this one. Whole seconds: get_settings reads it with int().
    with get_database_connection() as conn:
        save_settings_partial(conn, {'sessions_invalidated_at': int(time.time())})
        ended = close_sessions(conn, 'forced', session_config()['session_timeout_minutes'])
    core.reload_session_config()
    logger.info("Force re-login by %s: %d sessions ended", user, ended)
    return _user_redirect("user_activity", source="this", relogin=1)


_REDIRECT_TARGETS = {
    "users":       "/settings/users",
    "user_activity": "/settings/user-activity",
    "page_names":  "/settings/page-names",
}

_PAGE_NAME_MAX = 100


def _categories_in_order(stored: dict) -> list[str]:
    """Categories in use, in their saved order, then any others A-Z."""
    in_use = {v for k, v in stored.items() if k.startswith(core.CATEGORY_PREFIX)}
    positions = sorted(
        (int(k[len(core.CATEGORY_POS_PREFIX):]), v) for k, v in stored.items()
        if k.startswith(core.CATEGORY_POS_PREFIX) and k[len(core.CATEGORY_POS_PREFIX):].isdigit()
    )
    saved = [v for _, v in positions if v in in_use]
    return saved + sorted(in_use - set(saved))


def _order_number(form, field: str):
    """A whole number from 1 to 999, or None when blank or not a number."""
    value = str(form.get(field, "")).strip()
    return int(value) if value.isdigit() and 1 <= int(value) <= 999 else None


@router.get("/settings/page-names", response_class=HTMLResponse)
def page_names_page(request: Request, user: str = Depends(get_current_user)):
    """One box per page, holding its current name. Blank = the default name."""
    if not is_admin(user):
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
        "category_prefix": core.CATEGORY_PREFIX,
        "order_prefix": core.ORDER_PREFIX,
        "hidden_prefix": core.HIDDEN_PREFIX,
        "categories_in_use": _categories_in_order(stored),
        "sections": SETTINGS_SECTIONS, "active_section": "page_names",
    })


@router.post("/settings/page-names")
async def save_page_names_route(request: Request, user: str = Depends(get_current_user)):
    """Fields arrive as name__<page_key> and category__<page_key>. A blank
    box, or one holding the default name, stores nothing, so that page follows
    its default. Categories are stored in the same table as cat__<page_key>:
    save_page_names replaces every row, so names, categories and the site name
    must all be saved together here."""
    if not is_admin(user):
        raise HTTPException(status_code=403)
    form = await request.form()
    names = {}
    for page in PAGES:
        label = str(form.get(f"name__{page.key}", "")).strip()
        category = str(form.get(f"category__{page.key}", "")).strip()
        if len(label) > _PAGE_NAME_MAX or len(category) > _PAGE_NAME_MAX:
            return _user_redirect("page_names", nameerr=f"Names+must+be+{_PAGE_NAME_MAX}+characters+or+fewer")
        if label and label != page.label:
            names[page.key] = label
        if category:
            names[core.CATEGORY_PREFIX + page.key] = category
        order = _order_number(form, f"order__{page.key}")
        if order is not None:
            names[core.ORDER_PREFIX + page.key] = str(order)
        if form.get(f"hidden__{page.key}"):
            names[core.HIDDEN_PREFIX + page.key] = '1'
    # Category order: the boxes on the page are catorder__<n> (category name in
    # catname__<n>). Categories with no number, or new ones typed in this save,
    # go after the numbered ones.
    used = []
    for page in PAGES:
        c = names.get(core.CATEGORY_PREFIX + page.key)
        if c and c not in used:
            used.append(c)
    numbered = {}
    for key in form.keys():
        if key.startswith("catname__"):
            name = str(form.get(key, "")).strip()
            n = _order_number(form, "catorder__" + key[len("catname__"):])
            if name in used and n is not None:
                numbered[name] = n
    ranked = sorted(used, key=lambda c: (c not in numbered, numbered.get(c, 0), used.index(c)))
    for position, name in enumerate(ranked, start=1):
        names[core.CATEGORY_POS_PREFIX + str(position)] = name
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
    if redirect_to == "users":
        # Users page opens on prod; changes are made here, so show this environment.
        params = {"source": "this", **params}
    qs ="&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(url=f"{base}?{qs}", status_code=303)


@router.get("/settings/page-access")
def page_access_page(user: str = Depends(get_current_user)):
    """Page access moved to the Edit popup on User Accounts. Keeps old links working."""
    if not is_admin(user):
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    return RedirectResponse(url=BASE_URL + "/settings/users", status_code=303)


@router.get("/settings/users/page-access", response_class=HTMLResponse)
def page_access_batch_page(request: Request, user: str = Depends(get_current_user)):
    """Settings > Users > Page Access: pick one page, tick the users who have it."""
    if not is_admin(user):
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    can_view_prod, source = _users_source(request.query_params)
    viewing_prod = can_view_prod and source == 'prod'
    people, stored_access, error = {}, None, None
    try:
        if viewing_prod:
            people, _logins, _buyers, stored_access, _permissions = _read_prod_users()
        else:
            with get_database_connection() as conn:
                people = load_user_details(conn)
                ensure_user_nexus_columns(conn)
                for username, extra in read_user_nexus_details(conn).items():
                    if username in people:
                        people[username].update(extra)
                stored_access = load_all_page_access(conn)
    except Exception as e:
        logger.warning("Failed to load page access: %s", e)
        error = str(getattr(e, 'detail', None) or e)[:300]
    usernames = sorted(people) if viewing_prod else sorted(VALID_USERS)
    usernames = [u for u in usernames if u not in ADMIN_USERNAMES]
    access = {u: shown_page_access(stored_access.get(u))
              for u in usernames} if stored_access is not None else {}
    pages = core.visible_pages()
    wanted = request.query_params.get('page')
    selected = wanted if wanted in {p.key for p in pages} else (pages[0].key if pages else '')
    return templates.TemplateResponse("page_access.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "usernames": usernames, "people": people, "access": access,
        "access_loaded": stored_access is not None, "error": error,
        "pages": pages, "selected_page": selected,
        "can_view_prod": can_view_prod, "source": source, "viewing_prod": viewing_prod,
        "this_env": core._ENV,
        "sections": SETTINGS_SECTIONS, "active_section": "page_access",
    })


@router.post("/settings/users/page-access")
def page_access_batch_save(data: dict, user: str = Depends(get_current_user)):
    """Give one page to the ticked users and take it from the unticked ones.
    data {source, page_key, usernames}. Each user's other pages are left as they
    are, and all the changes save together or not at all."""
    if not is_admin(user):
        raise HTTPException(status_code=403)
    page_key = data.get('page_key')
    ticked = data.get('usernames')
    if page_key not in {p.key for p in core.visible_pages()}:
        raise HTTPException(status_code=400, detail="Unknown page")
    if not isinstance(ticked, list):
        raise HTTPException(status_code=400, detail="Bad user list")
    ticked = set(ticked)
    to_prod = _writes_to_prod(str(data.get('source') or 'this'))

    with (get_prod_activity_connection() if to_prod else get_database_connection()) as conn:
        try:
            known = set(read_user_details(conn) if to_prod else VALID_USERS) - ADMIN_USERNAMES
            if ticked - known:
                raise HTTPException(status_code=400, detail="Unknown user: " + ', '.join(sorted(ticked - known)))
            stored = load_all_page_access(conn)
            changes = {}
            for username in known:
                current = shown_page_access(stored.get(username))
                keys = current | {page_key} if username in ticked else current - {page_key}
                if keys != current:
                    changes[username] = keys
            set_page_access_many(conn, changes, granted_by=user)
        except Exception:
            conn.rollback()
            raise
    if not to_prod:
        for username, keys in changes.items():
            core.USER_PAGE_ACCESS[username] = keys | {CONFIGURED_MARKER}
    logger.info("Page access for %s changed for %s%s by %s", page_key, sorted(changes),
                " on PROD" if to_prod else "", user)
    return {"status": "ok", "changed": len(changes)}


def _save_user_access(conn, target: str, ticked: set | None, user: str, is_new: bool = False) -> set | None:
    """Save the Page Access ticks from the Add / Edit popup. Returns what was
    saved, or None when nothing changed. ticked None means the popup did not show
    the ticks: an existing user is left alone, a new one gets no pages.
    Only writes a real change, so granted_at/granted_by keep showing when access
    last changed. Hidden pages have no tick box, so nothing arrives for them: keep
    what the user already had, ready for when they are unhidden."""
    if ticked is None:
        if not is_new:
            return None
        ticked = set()
    current = set()
    if not is_new:
        current = shown_page_access(load_all_page_access(conn).get(target))
    keys = ticked | (current & (ALL_PAGE_KEYS - {p.key for p in core.visible_pages()}))
    if keys == current and not is_new:
        return None
    set_page_access_many(conn, {target: keys}, granted_by=user)
    logger.info("Page access for %s set to %s by %s", target, sorted(keys) or 'none', user)
    return keys


def _writes_to_prod(source: str) -> bool:
    """True when local or dev is changing prod's users (the Users page's Prod
    view). On prod itself its own database already is prod."""
    return source == 'prod' and core._ENV != 'prod'


def _prod_users_redirect(**params):
    return _user_redirect("users", source="prod", **params)


def _end_prod_sessions(conn, reason: str, target: str) -> int:
    """End a prod user's open sessions after a delete. Only
    when prod's session table is in the current format, so nothing on prod is
    created or converted from here; prod's own sign-in check covers the rest."""
    try:
        if not user_sessions_ready(conn):
            return 0
        timeout = read_session_settings(conn)['session_timeout_minutes']
        return close_sessions(conn, reason, timeout, username=target, ensure=False)
    except Exception as e:
        conn.rollback()
        logger.warning("Could not end prod sessions for %s (%s): %s", target, reason, e)
        return 0


def _save_prod_user(mode: str, target: str, details: dict, user: str, is_buyer: bool = False,
                    org: dict = None, ticked: set = None, permissions: set = None):
    """save_user against prod's database. Prod's containers pick the change up
    when their user cache next reloads (within about a minute)."""
    if mode not in ('add', 'edit'):
        raise HTTPException(status_code=400, detail="Unknown mode")
    with get_prod_activity_connection() as conn:
        try:
            exists = target in read_user_details(conn)
            if mode == 'add' and exists:
                return _prod_users_redirect(pwderr="User+already+exists+on+prod")
            if mode == 'edit' and not exists:
                return _prod_users_redirect(pwderr="User+not+found+on+prod")
            if mode == 'add':
                add_user(conn, target)
            update_user_details(conn, {target: details}, ensure=False)
            set_user_role(conn, target, 'buyer', is_buyer)
            if permissions is not None:
                set_user_permissions(conn, target, permissions)
            update_user_org(conn, target, org or {})
            _save_user_access(conn, target, ticked, user, is_new=mode == 'add')
        except Exception:
            conn.rollback()
            raise
    logger.info("PROD user %s %s by %s", target, 'created' if mode == 'add' else 'saved', user)
    if mode == 'add':
        return _prod_users_redirect(useradded=target)
    return _prod_users_redirect(saved=target)


@router.post("/settings/user/save")
def save_user(
    request: Request,
    mode: str = Form(...),
    username: str = Form(...),
    first_name: str = Form(default=""),
    last_name: str = Form(default=""),
    email: str = Form(default=""),
    source: str = Form(default="this"),
    is_buyer: str = Form(default=""),
    site_name: str = Form(default=""),
    site_type: str = Form(default=""),
    company_name: str = Form(default=""),
    company_type: str = Form(default=""),
    access: list[str] = Form(default=[]),
    access_shown: str = Form(default=""),
    permissions: list[str] = Form(default=[]),
    permissions_shown: str = Form(default=""),
    user: str = Depends(get_current_user),
):
    """The Add / Edit user popup on Settings > User Accounts.
    mode 'add' needs an email address: it is how the person is matched to
    their Keycloak account at sign-in.
    access holds one page key per ticked box; access_shown says the ticks were
    on the page, so none ticked means no pages rather than leave as it is.
    permissions and permissions_shown work the same way.
    source 'prod' (local and dev only) saves to prod's database instead."""
    if not is_admin(user):
        raise HTTPException(status_code=403)
    to_prod = _writes_to_prod(source)
    back = _prod_users_redirect if to_prod else (lambda **p: _user_redirect("users", **p))
    target = username.strip().lower() if mode == 'add' else username.strip()
    if not target:
        return back(pwderr="Username+cannot+be+empty")
    if target in ADMIN_USERNAMES:
        return back(pwderr="Admin+accounts+are+set+in+code")
    details = {'first_name': first_name.strip(), 'last_name': last_name.strip(), 'email': email.strip()}
    if mode == 'add' and '@' not in details['email']:
        return back(pwderr="An+email+address+is+needed+to+sign+in")
    buyer = is_buyer == "on"
    org = {'site_name': site_name[:255], 'site_type': site_type[:100],
           'company_name': company_name[:255], 'company_type': company_type[:100]}
    ticked = set(access) if access_shown else None
    # Anything not a real page is rejected rather than ignored — this is also
    # what stops CONFIGURED_MARKER being submitted as if it were a page.
    if ticked and ticked - ALL_PAGE_KEYS:
        logger.warning("Page access: rejected unknown keys %s for %s", sorted(ticked - ALL_PAGE_KEYS), target)
        return back(pwderr="Unknown+page")
    held = set(permissions) if permissions_shown else None
    if held and held - set(USER_PERMISSIONS):
        logger.warning("Permissions: rejected unknown keys %s for %s", sorted(held - set(USER_PERMISSIONS)), target)
        return back(pwderr="Unknown+permission")
    if to_prod:
        return _save_prod_user(mode, target, details, user, buyer, org, ticked, held)

    if mode == 'add':
        if target in VALID_USERS:
            return _user_redirect("users", pwderr="User+already+exists")
        with get_database_connection() as conn:
            add_user(conn, target)
            update_user_details(conn, {target: details})
            set_user_role(conn, target, 'buyer', buyer)
            if held is not None:
                set_user_permissions(conn, target, held)
            ensure_user_nexus_columns(conn)
            update_user_org(conn, target, org)
            saved_access = _save_user_access(conn, target, ticked, user, is_new=True)
        VALID_USERS.add(target)
        core.USER_PAGE_ACCESS[target] = saved_access | {CONFIGURED_MARKER}
        if held is not None:
            core.USER_PERMISSIONS_HELD[target] = held
        core.reload_user_details()
        logger.info("User %s created by %s", target, user)
        return _user_redirect("users", useradded=target)

    if mode != 'edit':
        raise HTTPException(status_code=400, detail="Unknown mode")
    if target not in VALID_USERS:
        return _user_redirect("users", pwderr="User+not+found")
    with get_database_connection() as conn:
        update_user_details(conn, {target: details})
        set_user_role(conn, target, 'buyer', buyer)
        if held is not None:
            set_user_permissions(conn, target, held)
            # This container straight away; others within PERMISSION_TTL_SECONDS.
            core.USER_PERMISSIONS_HELD[target] = held
        ensure_user_nexus_columns(conn)
        update_user_org(conn, target, org)
        saved_access = _save_user_access(conn, target, ticked, user)
        if saved_access is not None:
            # This container straight away; others within PERMISSION_TTL_SECONDS.
            core.USER_PAGE_ACCESS[target] = saved_access | {CONFIGURED_MARKER}
    core.reload_user_details()   # this container's account menu; others within a minute
    logger.info("User %s saved by %s", target, user)
    return _user_redirect("users", saved=target)


@router.post("/settings/delete-user")
def delete_user(
    request: Request,
    target_user: str = Form(...),
    redirect_to: str = Form("settings"),
    source: str = Form(default="this"),
    user: str = Depends(get_current_user)
):
    if not is_admin(user):
        raise HTTPException(status_code=403)
    if target_user in ADMIN_USERNAMES:
        raise HTTPException(status_code=400, detail="Cannot delete admin")
    if _writes_to_prod(source):
        with get_prod_activity_connection() as conn:
            try:
                if target_user not in read_user_details(conn):
                    return _prod_users_redirect(pwderr="User+not+found+on+prod")
                delete_user_and_access(conn, target_user)
            except Exception:
                conn.rollback()
                raise
            ended = _end_prod_sessions(conn, 'user_deleted', target_user)
        logger.info("PROD user %s deleted by %s, %d sessions ended", target_user, user, ended)
        return _prod_users_redirect(userdeleted=target_user)
    if target_user not in VALID_USERS:
        return _user_redirect(redirect_to, pwderr="User+not+found")
    # One transaction for the account and its page access — a separate clear
    # could fail after the account was gone, leaving grants for a recreated
    # username to inherit.
    ended = 0
    with get_database_connection() as conn:
        delete_user_and_access(conn, target_user)
        try:
            ended = close_sessions(conn, 'user_deleted', session_config()['session_timeout_minutes'],
                                   username=target_user)
        except Exception as e:
            # The account is gone either way; get_current_user signs them out.
            logger.warning("Could not end sessions for deleted user %s: %s", target_user, e)
    VALID_USERS.discard(target_user)
    core.USER_PAGE_ACCESS.pop(target_user, None)
    logger.info("User %s deleted by %s, %d sessions ended", target_user, user, ended)
    return _user_redirect(redirect_to, userdeleted=target_user)


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
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    calc = calc_buying_reqs == "on"
    gs = get_stock == "on"
    ecb = enable_cars_bought == "on"
    kai = kpi_ai_summary == "on"
    coe = cap_override_enabled == "on"
    with get_database_connection() as conn:
        save_settings(conn, number_to_buy, max_days_sales, max_dts, min_gpu_m, cap_workers, cap_rate_limit, calc, gs, enable_cars_bought=ecb, kpi_ai_summary=kai, mot_workers=mot_workers, at_workers=at_workers, at_rate_limit=at_rate_limit, valuation_cache_hours=valuation_cache_hours, cap_override_enabled=coe, cap_override_subscriber_id=cap_override_subscriber_id.strip(), cap_override_password=cap_override_password.strip(), user='buyer')
    # Reset CAPClient singleton so the next task picks up the new creds
    from clients.cap_client import CAPClient
    from utils.singleton import Singleton
    Singleton._instances.pop(CAPClient, None)
    logger.info(
        "Settings updated by %s: number_to_buy=%d, max_days_sales=%d, max_dts=%d, "
        "min_gpu_m=%d, cap_workers=%d, cap_rate_limit=%d, at_workers=%d, at_rate_limit=%d, calc_buying_reqs=%s, get_stock=%s, enable_cars_bought=%s, mot_workers=%d",
        user, number_to_buy, max_days_sales, max_dts, min_gpu_m, cap_workers, cap_rate_limit, at_workers, at_rate_limit, calc, gs, ecb, mot_workers
    )
    return RedirectResponse(url=BASE_URL + "/settings-page?saved=1", status_code=303)
