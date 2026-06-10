"""Settings page, user management and app settings (admin only)."""

from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import time
import hashlib
import logging


from utils.db_utils import (get_database_connection, get_settings, save_settings,
                            get_last_login_per_user, get_active_sessions_all_envs,
                            upsert_user, remove_user)

from . import core
from .core import (BASE_URL, templates, VALID_USERS, _active_sessions, get_current_user)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/settings-page", response_class=HTMLResponse)
def settings_page(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    with get_database_connection() as conn:
        settings = get_settings(conn, user='buyer')
    sessions = sorted(_active_sessions.values(), key=lambda s: s['login_time'], reverse=True)
    non_admin_users = sorted(u for u in VALID_USERS if u != 'tdrayson')
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "BASE_URL": BASE_URL, "settings": settings, "active_sessions": sessions, "non_admin_users": non_admin_users})


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
