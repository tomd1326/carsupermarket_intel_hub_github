"""Live users: initial circles beside the Home button.

Every signed-in tab sends a heartbeat with the page it is on. Who is shown:
  - tdrayson sees everyone on the same page, himself included. On Home he
    sees everyone signed in on any page, with the pages they have open;
  - everyone else sees only tdrayson, when he is on the same page.
The rule is applied here, so a direct call to the endpoint cannot get more.
"""
import json
import logging
import re

from fastapi import APIRouter, Request, HTTPException

from utils.db_utils import (get_database_connection, touch_page_presence, mark_page_presence_left)

from .core import _ENV, get_current_user, is_admin, presence_config, page_name, ADMIN_USERNAME
from .page_registry import PAGES

logger = logging.getLogger(__name__)

router = APIRouter()

_TAB_ID_RE = re.compile(r'^[A-Za-z0-9-]{8,64}$')
# % allows escaped characters, e.g. a space in a VRM on /assess-vehicle/vehicle/...
_PAGE_RE = re.compile(r'^/[A-Za-z0-9/_.%~\-]{0,199}$')


async def _read_body(request: Request) -> dict:
    """JSON body, whatever the content type: the leave call comes from
    navigator.sendBeacon, which cannot set one."""
    try:
        body = json.loads(await request.body() or b'{}')
    except ValueError:
        raise HTTPException(status_code=400)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400)
    return body


def _tab_id(body: dict) -> str:
    tab_id = str(body.get('tab_id', ''))
    if not _TAB_ID_RE.match(tab_id):
        raise HTTPException(status_code=400)
    return tab_id


def _initials(username: str, first: str | None, last: str | None) -> str:
    """Name initials only when both names are filled in. A first name alone
    would give e.g. 'TO' for Tom; the username gives 'TD' for tdrayson."""
    first, last = (first or '').strip(), (last or '').strip()
    if first and last:
        return (first[0] + last[0]).upper()
    return username[:2].upper()


def _seq(body: dict) -> int:
    """Each tab numbers its messages 1, 2, 3... so a late one can be ignored."""
    try:
        seq = int(body.get('seq', 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400)
    if seq < 1 or seq > 2_000_000_000:
        raise HTTPException(status_code=400)
    return seq


_HOME_PAGE = '/home'


def _page_label(path: str) -> str:
    """Name of the page a tab is on, as set on Settings > Page Names. The
    longest matching path wins, so /assess-vehicle/vehicle/AB12CDE is Assess
    Vehicle."""
    if path == _HOME_PAGE:
        return 'Home'
    if path.startswith('/settings'):
        return 'Settings'
    if path == '/my-account':
        return 'My Account'
    matches = [p for p in PAGES if path == p.path or path.startswith(p.path + '/')]
    if matches:
        return page_name(max(matches, key=lambda p: len(p.path)).key)
    return path


# Sent with every heartbeat so open pages pick up Settings > Live Users
# changes on their next check, without a reload.
_DISPLAY_KEYS = ('presence_interval_seconds', 'presence_max_circles', 'presence_circle_size',
                 'presence_border_width', 'presence_border_colour', 'presence_gap',
                 'presence_text_colour', 'presence_more_colour', 'presence_ring_width', 'presence_ring_gap')


@router.post("/api/presence")
async def presence_heartbeat(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await _read_body(request)
    tab_id = _tab_id(body)
    seq = _seq(body)
    page = str(body.get('page', ''))
    if not _PAGE_RE.match(page):
        raise HTTPException(status_code=400)

    config = presence_config()
    display = {k: config[k] for k in _DISPLAY_KEYS}
    if not config['presence_enabled']:
        return {"users": [], "config": display}

    only_user = None if is_admin(user) else ADMIN_USERNAME
    all_pages = is_admin(user) and page == _HOME_PAGE
    with get_database_connection() as conn:
        rows = touch_page_presence(conn, tab_id, seq, request.session.get('session_id', ''), user, _ENV,
                                   page, config['presence_drop_off_seconds'], only_user, all_pages)
    users = []
    for username, first, last, pages in rows:
        full_name = ' '.join(p for p in ((first or '').strip(), (last or '').strip()) if p)
        users.append({
            "username": username,
            "initials": _initials(username, first, last),
            "name": full_name or username,
            # Only on Home: everywhere else everyone listed is on this page.
            "pages": sorted({_page_label(p) for p in pages or []}) if all_pages else None,
        })
    # The viewer's own circle first, the way Google shows it.
    users.sort(key=lambda u: u["username"] != user)
    return {"users": users, "me": user, "config": display}


@router.post("/api/presence/leave")
async def presence_leave(request: Request):
    user = get_current_user(request)
    if not user:
        return {"ok": True}   # signed out: nothing of theirs left to show
    body = await _read_body(request)
    tab_id = _tab_id(body)
    seq = _seq(body)
    try:
        with get_database_connection() as conn:
            mark_page_presence_left(conn, tab_id, seq, request.session.get('session_id', ''), user, _ENV)
    except Exception as e:
        # Not worth an error: the row drops off on its own after the drop-off time.
        logger.warning("Presence leave failed for %s: %s", user, e)
    return {"ok": True}
