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
    sharepoint_cache_minutes: int = Form(...),
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
            'sharepoint_cache_minutes': max(0, sharepoint_cache_minutes),
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
            **colours,
        })
    # This container uses the new values straight away; others within a minute.
    core.reload_presence_config()
    logger.info("Settings [Live Users] updated by %s", user)
    return _section_redirect("live-users")


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
