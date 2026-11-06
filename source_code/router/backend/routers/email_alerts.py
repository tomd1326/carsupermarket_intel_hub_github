"""Settings > Email Alerts: who gets each automatic email, admin only.

Each alert needs code that decides when to send it, so the list of alerts is fixed here
(ALERTS). Who receives each one, and whether it is on, is set in the page and kept in
kpi_layout under 'email_alerts'. Recipients are users with an email address on
Settings > User Accounts. send_alert() is what the code behind an alert calls.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException

from utils import email_client, page_alerts
from utils.db_utils import get_database_connection, read_user_details

from .core import get_current_user, is_admin, ADMIN_USERNAME
from . import kpi

logger = logging.getLogger(__name__)

router = APIRouter()

_KEY = 'email_alerts'
_API = '/api/email-alerts'

# key -> (name, when it is sent). Add a line here for a new alert, and call send_alert(key, ...).
ALERTS = {
    'cap_hpi_new_report': ('CAP HPI New Report',
                           'A check on Settings > External Data stored a new CAP HPI Car Market Overview '
                           'or Future Car Market Overview report.'),
    'smmt_new_file': ('SMMT New File',
                      'A check on Settings > External Data stored a new SMMT file (registrations, used car '
                      'sales or outlook).'),
}


def _admin(user):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")


def _config() -> dict:
    """{alert key: {'enabled': bool, 'users': [username]}}; a new alert starts on, for Tom only."""
    stored = kpi._read_kpi_layout_value(_KEY)
    stored = stored if isinstance(stored, dict) else {}
    out = {}
    for key in ALERTS:
        s = stored.get(key) if isinstance(stored.get(key), dict) else {}
        out[key] = {'enabled': bool(s.get('enabled', True)),
                    'users': [u for u in s.get('users', [ADMIN_USERNAME]) if isinstance(u, str)]}
    return out


def _people() -> dict:
    with get_database_connection() as conn:
        return read_user_details(conn)


def recipients(key: str) -> list:
    """Email addresses for an alert, or [] when it is off or nobody with an email is on it."""
    cfg = _config().get(key)
    if not cfg or not cfg['enabled']:
        return []
    people = _people()
    return sorted({people[u]['email'] for u in cfg['users'] if people.get(u, {}).get('email')})


def send_alert(key: str, subject: str, text: str) -> list:
    """Send one alert as plain text (links made clickable). Returns who it went to."""
    to = recipients(key)
    if not to:
        logger.info("Email alert %s not sent: off or no recipients", key)
        return []
    email_client.send_email(to, f"Intelligence Hub: {subject}", email_client.text_to_html(text))
    logger.info("Email alert %s sent to %d people", key, len(to))
    return to


@router.get(_API)
def email_alerts_list(user: str = Depends(get_current_user)):
    _admin(user)
    try:
        people = _people()
        users = sorted(({'username': u, 'name': f"{d['first_name']} {d['last_name']}".strip() or u,
                         'email': d['email']} for u, d in people.items()), key=lambda p: p['name'].lower())
        alerts = [{'key': k, 'name': n, 'description': d} for k, (n, d) in ALERTS.items()]
        return {'status': 'ok', 'alerts': alerts, 'config': _config(), 'users': users}
    except Exception as e:
        logger.error("Email alerts read failed: %s", e)
        return {'status': 'error', 'message': str(e)}


@router.post(_API)
def email_alerts_save(payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    incoming = (payload or {}).get('config') or {}
    try:
        known = set(_people())
        cfg = _config()
        for key, value in incoming.items():
            if key in cfg and isinstance(value, dict):
                cfg[key] = {'enabled': bool(value.get('enabled')),
                            'users': sorted({u for u in value.get('users') or [] if u in known})}
        kpi._write_kpi_layout_value(_KEY, cfg)
        logger.info("Email alerts saved by %s", user)
        return {'status': 'ok', 'config': cfg}
    except Exception as e:
        logger.error("Email alerts save failed: %s", e)
        return {'status': 'error', 'message': str(e)}


@router.get(_API + "/style")
def email_alert_style(user: str = Depends(get_current_user)):
    """The look shared by every page alert (see utils/page_alerts.py)."""
    _admin(user)
    try:
        return {'status': 'ok', 'style': page_alerts.saved_style(kpi._read_kpi_layout_value(page_alerts.STYLE_KEY)),
                'meta': page_alerts.style_meta()}
    except Exception as e:
        logger.error("Alert email style read failed: %s", e)
        return {'status': 'error', 'message': str(e)}


@router.post(_API + "/style")
def email_alert_style_save(payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    style = (payload or {}).get('style')
    err = page_alerts.validate_style(style, whole=True)
    if err:
        return {'status': 'error', 'message': err}
    try:
        kpi._write_kpi_layout_value(page_alerts.STYLE_KEY, style)
        logger.info("Alert email style saved by %s", user)
        return {'status': 'ok'}
    except Exception as e:
        logger.error("Alert email style save failed: %s", e)
        return {'status': 'error', 'message': str(e)}


@router.post(_API + "/test")
def email_alerts_test(payload: dict, user: str = Depends(get_current_user)):
    """Send a test of one alert to the people on it."""
    _admin(user)
    key = (payload or {}).get('key')
    if key not in ALERTS:
        return {'status': 'error', 'message': 'No such alert'}
    try:
        to = send_alert(key, f"Test of {ALERTS[key][0]}",
                        f"This is a test of the {ALERTS[key][0]} alert, sent from Settings > Email Alerts.")
        if not to:
            return {'status': 'error', 'message': 'The alert is off or nobody on it has an email address'}
        return {'status': 'ok', 'sent_to': to}
    except Exception as e:
        logger.error("Email alert test failed: %s", e)
        return {'status': 'error', 'message': str(e)}
