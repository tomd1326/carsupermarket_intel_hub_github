"""Sign-in with Keycloak (single sign-on), logout and root redirect.

/login sends the browser to Keycloak; Keycloak sends it back to
/login/oauth2/callback (the redirect URI registered on the client) with a code.
The callback checks the ID token, matches its email to exactly one row of the
users table, and starts the same session the old password login did.
"""

import logging
import os
import time

from fastapi import APIRouter, Request, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from utils.db_utils import (get_database_connection, record_login, record_logout,
                            find_usernames_by_email, remove_session_presence, new_session_id)
from utils import keycloak_auth

from .core import (BASE_URL, _ENV, templates, VALID_USERS, _reload_users_from_aurora,
                   get_current_user, session_config, is_local_app)

logger = logging.getLogger(__name__)

router = APIRouter()


def _login_page(request: Request, error: str | None = None, status_code: int = 200):
    """The only page a signed-out person sees: why sign-in did not work, or a
    button to try again."""
    return templates.TemplateResponse(
        "login.html", {"request": request, "BASE_URL": BASE_URL, "error": error},
        status_code=status_code)


def _start_session(request: Request, username: str) -> None:
    session_id = new_session_id()
    login_epoch = time.time()
    request.session.clear()   # nothing carried over from an earlier login's cookie
    request.session['user'] = username
    request.session['session_id'] = session_id
    request.session['login_time_epoch'] = login_epoch
    try:
        with get_database_connection() as conn:
            record_login(conn, session_id, username, _ENV)
        request.session['seen_epoch'] = login_epoch
    except Exception as e:
        # No seen_epoch, so the next request makes the row instead.
        logger.warning("Failed to record login for %s: %s", username, e)


def _local_bypass_user() -> str | None:
    """The user to sign in as without Keycloak, for working on your own
    computer. Needs BOTH LOCAL_ENV=1 and no AWS Lambda around, so it cannot be
    switched on in a deployment by setting one variable."""
    if os.getenv('LOCAL_ENV') != '1' or not is_local_app():
        return None
    return os.getenv('LOCAL_LOGIN_AS') or None


def _username_for_email(email: str) -> tuple[str | None, str | None]:
    """(username, None) when the email belongs to exactly one user, otherwise
    (None, the reason to show)."""
    _reload_users_from_aurora()
    with get_database_connection() as conn:
        usernames = [u for u in find_usernames_by_email(conn, email) if u in VALID_USERS]
    if not usernames:
        return None, ("Your Keycloak account is not linked to an Intelligence Hub user. "
                      "Ask an admin to add your email address.")
    if len(usernames) > 1:
        return None, ("More than one Intelligence Hub user has your email address, so we cannot tell "
                      "which one is you. Ask an admin to fix it.")
    return usernames[0], None


@router.get("/", response_class=HTMLResponse)
def root(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    return RedirectResponse(url=BASE_URL + "/home", status_code=303)


@router.get("/login")
async def login(request: Request, user: str = Depends(get_current_user)):
    if user:
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    bypass = _local_bypass_user()
    if bypass:
        await run_in_threadpool(_reload_users_from_aurora)
        if bypass in VALID_USERS:
            logger.info("Local sign-in as %s (LOCAL_LOGIN_AS, no Keycloak)", bypass)
            await run_in_threadpool(_start_session, request, bypass)
            return RedirectResponse(url=BASE_URL + "/home", status_code=303)
        return _login_page(request, f"LOCAL_LOGIN_AS is '{bypass}', which is not a user here.", 403)
    try:
        client = await run_in_threadpool(keycloak_auth.get_client, _ENV)
        return await client.authorize_redirect(request, keycloak_auth.redirect_uri(_ENV))
    except keycloak_auth.KeycloakNotConfigured as e:
        return _login_page(request, str(e), 503)
    except Exception:
        logger.exception("Could not start the Keycloak sign-in")
        return _login_page(request, "Single sign-on is not available right now. Please try again later.", 503)


@router.get("/login/oauth2/callback")
async def login_callback(request: Request):
    if request.query_params.get('error'):
        # The person cancelled, or Keycloak refused. The details stay in the log.
        logger.warning("Keycloak sign-in refused: %s %s", request.query_params.get('error'),
                       request.query_params.get('error_description'))
        return _login_page(request, "Sign-in was cancelled or refused.", 401)
    try:
        client = await run_in_threadpool(keycloak_auth.get_client, _ENV)
        token = await client.authorize_access_token(request)
    except keycloak_auth.KeycloakNotConfigured as e:
        return _login_page(request, str(e), 503)
    except Exception as e:
        # A bad or replayed state, an expired code, a failed signature check.
        logger.warning("Keycloak callback failed: %s", e)
        return _login_page(request, "Sign-in failed. Please try again.", 401)

    claims = token.get('userinfo') or {}
    email = (claims.get('email') or '').strip()
    if not email or claims.get('email_verified') is False:
        logger.warning("Keycloak sign-in without a verified email (sub=%s)", claims.get('sub'))
        return _login_page(request, "Keycloak did not give us a verified email address for you.", 403)
    try:
        username, problem = await run_in_threadpool(_username_for_email, email)
    except Exception:
        logger.exception("Could not look up the user for %s", email)
        return _login_page(request, "Could not check your account right now. Please try again.", 503)
    if username is None:
        logger.warning("Keycloak sign-in refused for %s: %s", email, problem)
        return _login_page(request, problem, 403)

    await run_in_threadpool(_start_session, request, username)
    logger.info("User %s signed in with Keycloak (%s)", username, email)
    return RedirectResponse(url=BASE_URL + "/home", status_code=303)


@router.get("/logout")
def logout(request: Request):
    """Ends this app's session only. The Keycloak session stays, so signing in
    again needs no password; the page after logout waits for a click instead of
    sending the browser straight back through Keycloak."""
    session_id = request.session.get('session_id')
    if session_id:
        try:
            with get_database_connection() as conn:
                record_logout(conn, session_id, session_config()['session_timeout_minutes'])
                remove_session_presence(conn, session_id)
        except Exception as e:
            logger.warning("Failed to record logout for session %s: %s", session_id, e)
    request.session.clear()
    return _login_page(request)
