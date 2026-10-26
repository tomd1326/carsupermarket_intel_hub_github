"""
Google login for the Weekly Business Review Slides export.

The website acts as one Google account (Tom's), connected once in
Settings > Google. tom.drayson@carsupermarket.com is a plain Google account
with no Shared drives, so a service account cannot own the decks; they are
created in the connected account's My Drive instead.

OAuth client: project `wbr-slides`, consent screen published "In production"
(in Testing, refresh tokens expire after 7 days). Scope `drive.file` only:
the app can reach just the files it creates, and Google does not review
non-sensitive scopes. `openid email` is only to show who is connected.

Secret api_access keys:
- GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET: the OAuth web client
- GOOGLE_TOKEN_KEY: Fernet key the refresh token is encrypted with

The refresh token is kept encrypted in the Aurora `settings` table of each
environment (key google_refresh_token), so each environment is connected
separately. It is never logged or sent to the page.
"""
import base64
import json
import logging
import os
import urllib.parse

import requests

from utils.secrets_manager_client import get_secret
from utils.db_utils import get_database_connection, save_settings_partial, widen_settings_values

logger = logging.getLogger(__name__)

SCOPES = ["openid", "email", "https://www.googleapis.com/auth/drive.file"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

TOKEN_SETTING = "google_refresh_token"
EMAIL_SETTING = "google_account_email"


class GoogleNotConnected(Exception):
    """No usable Google login. The message is safe to show on the page."""


def _secrets() -> dict:
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    return get_secret(f'{env}/carsupermarket/car_pricer/api_access')


def client_config() -> tuple[str, str]:
    s = _secrets()
    client_id, client_secret = s.get('GOOGLE_OAUTH_CLIENT_ID'), s.get('GOOGLE_OAUTH_CLIENT_SECRET')
    if not client_id or not client_secret:
        raise GoogleNotConnected("Google is not set up: add GOOGLE_OAUTH_CLIENT_ID and "
                                 "GOOGLE_OAUTH_CLIENT_SECRET to the api_access secret.")
    return client_id, client_secret


def _fernet():
    from cryptography.fernet import Fernet
    key = _secrets().get('GOOGLE_TOKEN_KEY')
    if not key:
        raise GoogleNotConnected("Google is not set up: add GOOGLE_TOKEN_KEY to the api_access secret.")
    return Fernet(key.encode())


def _read_settings() -> dict:
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM settings WHERE key IN (%s, %s)", (TOKEN_SETTING, EMAIL_SETTING))
            return dict(cur.fetchall())


def status() -> dict:
    """{connected, email} for the Settings page. Never includes the token."""
    try:
        values = _read_settings()
    except Exception as e:
        logger.warning(f"Google status not read: {e}")
        values = {}
    return {"connected": bool(values.get(TOKEN_SETTING)), "email": values.get(EMAIL_SETTING) or ""}


def redirect_uri(request) -> str:
    """The callback address Google sends the user back to. It must match one
    registered on the OAuth client exactly."""
    from routers.core import BASE_URL
    netloc = request.url.netloc
    scheme = "http" if netloc.split(":")[0] in ("127.0.0.1", "localhost") else "https"
    return f"{scheme}://{netloc}{BASE_URL}/settings/google/callback"


def auth_url(request, state: str) -> str:
    client_id, _ = client_config()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(request),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        # Always show the consent screen so Google returns a refresh token
        # even when this account has connected before.
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _email_from_id_token(id_token: str) -> str:
    # Read straight from Google's token response over TLS, so the payload is
    # only decoded for the address, not verified.
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email", "")
    except Exception:
        return ""


def connect(request, code: str) -> str:
    """Swap the code from Google's redirect for tokens and save the refresh
    token. Returns the connected account's email."""
    client_id, client_secret = client_config()
    resp = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri(request),
        "grant_type": "authorization_code",
    }, timeout=30)
    if resp.status_code != 200:
        logger.error(f"Google token exchange failed ({resp.status_code}): {resp.text[:300]}")
        raise GoogleNotConnected(f"Google refused the login ({resp.status_code}).")
    data = resp.json()
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        raise GoogleNotConnected("Google did not return a refresh token. Disconnect, then connect again.")
    email = _email_from_id_token(data.get("id_token", ""))
    encrypted = _fernet().encrypt(refresh_token.encode()).decode()
    with get_database_connection() as conn:
        widen_settings_values(conn)
        save_settings_partial(conn, {TOKEN_SETTING: encrypted, EMAIL_SETTING: email})
        conn.commit()
    return email


def _delete_saved() -> None:
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM settings WHERE key IN (%s, %s)", (TOKEN_SETTING, EMAIL_SETTING))
        conn.commit()


def disconnect() -> None:
    """Cancel the login at Google (best effort) and delete it here."""
    token = _saved_refresh_token(required=False)
    if token:
        try:
            requests.post(REVOKE_URL, params={"token": token}, timeout=15)
        except Exception as e:
            logger.warning(f"Google token revoke failed: {e}")
    _delete_saved()


def _saved_refresh_token(required: bool = True) -> str | None:
    encrypted = _read_settings().get(TOKEN_SETTING)
    if not encrypted:
        if required:
            raise GoogleNotConnected("Google is not connected - connect it in Settings > Google.")
        return None
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken:
        if required:
            raise GoogleNotConnected("The saved Google login cannot be read - reconnect in Settings > Google.")
        return None


def access_token() -> str:
    """A fresh access token for the connected account."""
    client_id, client_secret = client_config()
    resp = requests.post(TOKEN_URL, data={
        "refresh_token": _saved_refresh_token(),
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    }, timeout=30)
    if resp.status_code != 200:
        error = ""
        try:
            error = resp.json().get("error", "")
        except ValueError:
            pass
        logger.error(f"Google token refresh failed ({resp.status_code}): {error}")
        if error == "invalid_grant":
            raise GoogleNotConnected("Google disconnected - reconnect in Settings > Google.")
        raise GoogleNotConnected(f"Google login failed ({resp.status_code}).")
    return resp.json()["access_token"]
