"""Settings page, user management and app settings (admin only)."""

from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import time
import hashlib
import logging


from utils.db_utils import (get_database_connection, get_settings, save_settings,
                            save_settings_partial, get_last_login_per_user,
                            get_active_sessions_all_envs, upsert_user, remove_user,
                            ALWAYS_ENABLED_USERS)

from . import core
from .core import (BASE_URL, templates, VALID_USERS, _active_sessions, get_current_user)

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


@router.get("/settings/users", response_class=HTMLResponse)
def users_page(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    sessions = sorted(_active_sessions.values(), key=lambda s: s['login_time'], reverse=True)
    non_admin_users = sorted(u for u in VALID_USERS if u != 'tdrayson')
    last_logins = {}
    db_active_sessions = []
    try:
        with get_database_connection() as conn:
            last_logins = get_last_login_per_user(conn)
            db_active_sessions = get_active_sessions_all_envs(conn)
    except Exception as e:
        logger.warning("Failed to load user activity data: %s", e)
    return templates.TemplateResponse("users.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "active_sessions": sessions, "non_admin_users": non_admin_users,
        "last_logins": last_logins, "db_active_sessions": db_active_sessions,
        "sections": SETTINGS_SECTIONS, "active_section": "users",
    })


@router.post("/settings/force-relogin")
def force_relogin(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    # Rebind on the core module so get_current_user (which reads core's global) sees it
    core._sessions_invalidated_at = time.time()
    _active_sessions.clear()
    return RedirectResponse(url=BASE_URL + "/settings-page?relogin=1", status_code=303)


def _user_redirect(redirect_to: str, **params):
    base = BASE_URL + ("/settings/users" if redirect_to == "users" else "/settings-page")
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(url=f"{base}?{qs}", status_code=303)


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
    del VALID_USERS[target_user]
    with get_database_connection() as conn:
        remove_user(conn, target_user)
    logger.info("User %s deleted by admin", target_user)
    return _user_redirect(redirect_to, userdeleted=target_user)


@router.post("/settings/add-user")
def add_user(
    request: Request,
    new_username: str = Form(...),
    new_password: str = Form(...),
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
