from __future__ import annotations

import logging
import time
import requests

from utils.secrets_manager_client import get_secret
from utils.sharepoint_client import _TOKEN_CACHE

logger = logging.getLogger(__name__)

# Same app registration and secret the SharePoint client uses — one set of
# credentials for everything this app does against Microsoft Graph. Sending mail
# needs the Mail.Send APPLICATION permission granted on that registration by an
# Azure admin; without it Graph answers 403 and nothing is sent.
SECRET_NAME = "prod/carsupermarket/sharepoint/credentials"

# The mailbox mail is sent as. Graph sends on behalf of a real mailbox rather
# than an anonymous relay, so this address must exist in the tenant and the app
# must be allowed to send as it. Set in Settings > Email; this is only the
# fallback when that box is blank.
DEFAULT_SENDER = "tom.drayson@carsupermarket.com"


def configured_sender() -> str:
    """Sender address from Settings > Email, else DEFAULT_SENDER."""
    from utils.db_utils import get_database_connection, get_settings
    try:
        with get_database_connection() as conn:
            sender = get_settings(conn).get('email_sender', '')
    except Exception as e:
        logger.warning("Could not read email sender setting: %s", e)
        sender = ''
    return sender.strip() or DEFAULT_SENDER


def _get_token() -> str:
    """Client-credentials token for Graph, sharing SharePointClient's cache."""
    secret = get_secret(SECRET_NAME)
    tenant_id = secret["tenant_id"]
    client_id = secret["client_id"]
    client_secret = secret["client_secret"]

    cache_key = (tenant_id, client_id)
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and time.monotonic() < cached["expires"]:
        return cached["token"]

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    resp = requests.post(url, data={
        "client_id": client_id,
        "client_secret": client_secret.strip(),
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    token = body["access_token"]
    expires_in = int(body.get("expires_in", 3600))
    _TOKEN_CACHE[cache_key] = {"token": token, "expires": time.monotonic() + max(60, expires_in - 120)}
    return token


def send_email(to: str | list[str], subject: str, body_html: str,
               sender: str | None = None) -> None:
    """
    Send one email through Microsoft Graph.

    Raises on failure (requests.HTTPError) so the caller decides whether a
    failed send matters — it is never swallowed here.
    """
    recipients = [to] if isinstance(to, str) else list(to)
    if not recipients:
        raise ValueError("send_email needs at least one recipient")

    sender = sender or configured_sender()
    token = _get_token()
    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": a}} for a in recipients],
            },
            "saveToSentItems": True,
        },
        timeout=30,
    )
    # Graph returns 202 Accepted with an empty body on success. Surface the
    # response text on failure — its error message says whether the problem is
    # the permission, the sender mailbox, or the recipient.
    if resp.status_code >= 400:
        logger.error("Graph sendMail failed (%s): %s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    logger.info("Sent email '%s' to %s", subject, ", ".join(recipients))
