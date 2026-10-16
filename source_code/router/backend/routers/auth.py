"""Login, logout and root redirect."""

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
import os
import time
import hashlib
import logging


from utils.db_utils import (get_database_connection, record_login, record_logout,
                            find_usernames_by_email, get_settings, remove_session_presence)

from .core import (BASE_URL, _ENV, templates, VALID_USERS, _active_sessions, _reload_users_from_aurora,
                   get_current_user, _uk_now, ADMIN_USERNAME)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def root(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    #
    return RedirectResponse(url=BASE_URL + "/home", status_code=303)  # CHANGED: was /tasks
    # return templates.TemplateResponse("supplier_stock_evaluator.html", {"request": request, "user": user})


@router.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, 'BASE_URL': BASE_URL})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    _reload_users_from_aurora()
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    # Sign in with the email address, or the username too unless the
    # 'login_mode' setting is 'email'. Admin can always use its username so a
    # missing email can never lock everyone out.
    entered = username.strip()
    username = None
    try:
        with get_database_connection() as conn:
            login_mode = get_settings(conn).get('login_mode', 'both')
            if '@' in entered:
                # Several accounts can share an email - use the one the password fits.
                username = next((u for u in find_usernames_by_email(conn, entered)
                                 if VALID_USERS.get(u) == pwd_hash), None)
    except Exception as e:
        logger.warning("Login lookup failed: %s", e)
        login_mode = 'both'
    if username is None and (login_mode != 'email' or entered == ADMIN_USERNAME):
        username = entered
    if username and pwd_hash == VALID_USERS.get(username):
        session_id = hashlib.sha256(os.urandom(16)).hexdigest()
        login_epoch = time.time()
        request.session['user'] = username
        request.session['pwd_hash'] = pwd_hash
        request.session['session_id'] = session_id
        request.session['login_time_epoch'] = login_epoch
        _active_sessions[session_id] = {'user': username, 'login_time': _uk_now().strftime('%Y-%m-%d %H:%M:%S')}
        try:
            with get_database_connection() as conn:
                record_login(conn, session_id, username, _ENV, _uk_now())
        except Exception as e:
            logger.warning("Failed to record login for %s: %s", username, e)
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)  # CHANGED: was /tasks
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": "Invalid username or password",
            'BASE_URL': BASE_URL
        }
    )


@router.get("/logout")
def logout(request: Request):
    session_id = request.session.get('session_id')
    _active_sessions.pop(session_id, None)
    if session_id:
        try:
            with get_database_connection() as conn:
                record_logout(conn, session_id, _uk_now())
                remove_session_presence(conn, session_id)
        except Exception as e:
            logger.warning("Failed to record logout for session %s: %s", session_id, e)
    request.session.clear()
    return RedirectResponse(url=BASE_URL + "/login", status_code=303)
