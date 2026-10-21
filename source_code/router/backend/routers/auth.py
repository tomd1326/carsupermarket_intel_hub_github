"""Login, logout and root redirect."""

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
import time
import hashlib
import logging


from utils.db_utils import (get_database_connection, record_login, record_logout,
                            find_usernames_by_email, get_settings, remove_session_presence,
                            new_session_id, close_sessions, peek_password_link, redeem_password_link)
from utils.password_rules import PASSWORD_RULES, PASSWORD_MIN_LENGTH, password_error

from .core import (BASE_URL, _ENV, templates, VALID_USERS, _reload_users_from_aurora,
                   get_current_user, session_config, ADMIN_USERNAME)

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
    notice = "Your password is set. You can sign in now." if request.query_params.get('pwset') else None
    return templates.TemplateResponse("login.html", {"request": request, 'BASE_URL': BASE_URL, 'notice': notice})


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
        session_id = new_session_id()
        login_epoch = time.time()
        request.session.clear()   # nothing carried over from an earlier login's cookie
        request.session['user'] = username
        request.session['pwd_hash'] = pwd_hash
        request.session['session_id'] = session_id
        request.session['login_time_epoch'] = login_epoch
        try:
            with get_database_connection() as conn:
                record_login(conn, session_id, username, _ENV)
            request.session['seen_epoch'] = login_epoch
        except Exception as e:
            # No seen_epoch, so the next request makes the row instead.
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


def _set_password_page(request: Request, **context):
    """The set-password page. Never cached and never sends the address on to
    another site, because the address holds the link's token."""
    response = templates.TemplateResponse("set_password.html", {
        "request": request, "BASE_URL": BASE_URL, "rules": PASSWORD_RULES,
        "min_length": PASSWORD_MIN_LENGTH, **context})
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _peek_link(token: str):
    try:
        with get_database_connection() as conn:
            return peek_password_link(conn, token)
    except Exception as e:
        logger.warning("Could not check a password link: %s", e)
        return None


@router.get("/set-password", response_class=HTMLResponse)
def set_password_form(request: Request, token: str = ""):
    """Opened from an activation or reset link. Only looks at the link; it is
    used up when the new password is saved."""
    link = _peek_link(token)
    if link is None:
        return _set_password_page(request, invalid=True)
    return _set_password_page(request, token=token, username=link[0], purpose=link[1])


@router.post("/set-password", response_class=HTMLResponse)
def set_password_save(
    request: Request,
    token: str = Form(default=""),
    password: str = Form(default=""),
    confirm: str = Form(default=""),
):
    link = _peek_link(token)
    if link is None:
        return _set_password_page(request, invalid=True)
    username, purpose = link
    error = None
    if password != confirm:
        error = "The two passwords do not match"
    else:
        error = password_error(password)
    if error:
        return _set_password_page(request, token=token, username=username, purpose=purpose, error=error)
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    try:
        with get_database_connection() as conn:
            redeemed = redeem_password_link(conn, token, pwd_hash)
            if redeemed is not None:
                # The password is saved. Ending old sessions is a second step: if it
                # fails, get_current_user signs the old session out when it is next used.
                try:
                    close_sessions(conn, 'password_changed', session_config()['session_timeout_minutes'],
                                   username=username)
                except Exception as e:
                    logger.warning("Could not end sessions for %s after a password link: %s", username, e)
    except Exception as e:
        logger.warning("Could not save a password from a link: %s", e)
        return _set_password_page(request, token=token, username=username, purpose=purpose,
                                  error="The password could not be saved. Please try again")
    if redeemed is None:
        return _set_password_page(request, invalid=True)
    VALID_USERS[username] = pwd_hash
    logger.info("User %s set a password from a %s link", username, purpose)
    return RedirectResponse(url=BASE_URL + "/login?pwset=1", status_code=303)


@router.get("/logout")
def logout(request: Request):
    session_id = request.session.get('session_id')
    if session_id:
        try:
            with get_database_connection() as conn:
                record_logout(conn, session_id, session_config()['session_timeout_minutes'])
                remove_session_presence(conn, session_id)
        except Exception as e:
            logger.warning("Failed to record logout for session %s: %s", session_id, e)
    request.session.clear()
    return RedirectResponse(url=BASE_URL + "/login", status_code=303)
