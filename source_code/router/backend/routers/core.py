"""Shared state and helpers used by all domain routers.

Holds auth/session state (VALID_USERS, _active_sessions, _sessions_invalidated_at),
the Jinja2 templates instance, environment constants and event tracking.
Importing this module loads users from Aurora (same behaviour as the old
monolithic router.py import).
"""
import datetime
import logging
import os
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from utils.db_utils import get_database_connection, load_all_users, record_event

logger = logging.getLogger(__name__)


def _uk_now() -> datetime.datetime:
    return datetime.datetime.now(tz=ZoneInfo('Europe/London')).replace(tzinfo=None)


BASE_URL = os.getenv("BASE_URL", "/dev")
_ENV = 'local' if os.getenv('LOCAL_ENV') == '1' else ('prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev')
_S3_ENV = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'

# templates/ lives in the backend root, one level above this package
templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=templates_dir)

# Generic initialization
s3_bucketname = 'aa-raw-sources-uk'
s3_bucket_region = 'eu-west-1'  # bucket is always in eu-west-1 regardless of Lambda region


_ADMIN_FALLBACK = {
    'tdrayson': 'e026581a53dc97e3381566ad0f74e99ae65e0297f9eb41bb76596627885bf8a0',
}
VALID_USERS: dict = {}


def _reload_users_from_aurora():
    """Reload all users from Aurora into VALID_USERS. Admin is always valid regardless of DB state."""
    try:
        with get_database_connection() as conn:
            users = load_all_users(conn)
        VALID_USERS.clear()
        VALID_USERS.update(users)
        logger.info("Loaded %d users from Aurora", len(users))
    except Exception as e:
        logger.warning("Could not load users from Aurora — falling back to hardcoded admin only: %s", e)
    VALID_USERS.update(_ADMIN_FALLBACK)


_reload_users_from_aurora()

# session_id -> {user, login_time} — tracks active sessions server-side
_active_sessions: dict = {}
_sessions_invalidated_at: float = 0.0  # epoch — sessions logged in before this are force-logged out


def _track(request: Request, event_type: str, page: str, metadata: dict | None = None) -> None:
    session_id = request.session.get('session_id')
    username = request.session.get('user')
    if not session_id or not username:
        return
    try:
        with get_database_connection() as conn:
            record_event(conn, session_id, username, _ENV, event_type, page, metadata)
    except Exception as e:
        logger.warning("Failed to record event %s for %s: %s", event_type, username, e)


def get_current_user(request: Request):
    user = request.session.get('user')
    if not user:
        return None
    # Invalidate session if password has changed since login
    if request.session.get('pwd_hash') != VALID_USERS.get(user):
        _active_sessions.pop(request.session.get('session_id'), None)
        request.session.clear()
        return None
    # Invalidate session if admin triggered a force re-login after this session logged in
    if request.session.get('login_time_epoch', 0) < _sessions_invalidated_at:
        _active_sessions.pop(request.session.get('session_id'), None)
        request.session.clear()
        return None
    return user
