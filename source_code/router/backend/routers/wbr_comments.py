"""Weekly Business Review Comments, and the alerts behind the bell in the page
header.

Comments are discussion threads on a review chart, in the style of Google
Slides: a comment, replies under it, @mentions, an optional "Assigned to",
and Resolve. They are separate from Notes, which are business changes drawn
as dotted lines on the KPI charts.

A comment belongs to a chart, not to one placing of it: target is
'chart:<KPI row id>|<m|w|d>', so the same chart on the grid and on any slide
shows the same threads. A chart taken off the review keeps its comments; they
show again if it is put back. A Report table's comments have target
'table:<table id>'.

Who may do what (checked here, not only in the page):
  - anyone who can open the review may comment and reply;
  - only the author may edit a comment; the author or the admin may delete it
    (deleting a thread deletes its replies);
  - the thread's author, the person it is assigned to, or the admin may
    resolve or reopen it.

An @mention, or a reply to a thread someone started or was assigned, adds a
row to user_alerts for that person; the bell (static/js/alerts.js) shows it.
Only people who can open the review can be mentioned.

Both tables live in the PROD cluster, like kpi_layout, so the comments match
the charts they are on in every environment. db/21_wbr_comments.sql is the
same change for running by hand.
"""
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request

from utils.db_utils import get_prod_pricer_connection
from utils.ddl_once import once_per_database
from . import core
from .core import _track, get_current_user, is_admin, require_page_api, user_can, user_details

logger = logging.getLogger(__name__)
router = APIRouter()

PAGE = 'weekly_business_review'
# Vehicle notes (routers/vehicle_comments.py) share this table and these checks:
# target 'vehicle:<Autoview stock id>'. Anyone who can open one of these pages
# can read and write them. Add a page key here when a page gets the Notes column
# (static/js/vehicle_comments.js).
VEHICLE_PAGES = ('trade_report', 'orders_deliveries', 'stock_list')
VEHICLE_TARGET_RE = re.compile(r"^vehicle:([A-Za-z0-9_\-]{1,40})$")
BODY_MAX = 2000
_TARGET_RE = re.compile(r"^(?:chart:[A-Za-z0-9_\-]{1,40}\|[mwd]|table:[A-Za-z0-9_\-]{1,40})$")
_REVIEW_PREFIXES = ('chart:', 'table:')
# @username, where a username may itself be an email address.
_MENTION_RE = re.compile(r"@([A-Za-z0-9._\-]+(?:@[A-Za-z0-9.\-]+)?)")


@once_per_database
def _ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS wbr_comments (
                id          BIGSERIAL    PRIMARY KEY,
                target      VARCHAR(100) NOT NULL,
                parent_id   BIGINT       REFERENCES wbr_comments(id),
                author      VARCHAR(100) NOT NULL,
                body        TEXT         NOT NULL,
                mentions    TEXT[]       NOT NULL DEFAULT '{}',
                assigned_to VARCHAR(100),
                resolved_at TIMESTAMPTZ,
                resolved_by VARCHAR(100),
                deleted_at  TIMESTAMPTZ,
                created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                edited_at   TIMESTAMPTZ
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS wbr_comments_target ON wbr_comments (target) WHERE deleted_at IS NULL")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_alerts (
                id         BIGSERIAL    PRIMARY KEY,
                username   VARCHAR(100) NOT NULL,
                kind       VARCHAR(30)  NOT NULL,
                comment_id BIGINT       REFERENCES wbr_comments(id),
                from_user  VARCHAR(100),
                text       VARCHAR(300) NOT NULL,
                link       VARCHAR(500) NOT NULL,
                created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                read_at    TIMESTAMPTZ
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS user_alerts_user ON user_alerts (username, created_at DESC)")
    conn.commit()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _display_name(username: str) -> str:
    d = user_details(username)
    name = f"{d.get('first_name', '')} {d.get('last_name', '')}".strip()
    return name or username


def _pages_for(target: str) -> tuple:
    """The pages whose users may read and write comments on this target."""
    return VEHICLE_PAGES if str(target).startswith('vehicle:') else (PAGE,)


def _check_access(user: str, target: str) -> None:
    if not any(user_can(user, p) for p in _pages_for(target)):
        raise ValueError("You cannot open the page these comments are on")


def _commenters(pages: tuple = (PAGE,)) -> list:
    """Everyone who can open one of the pages: the @ list, and who can be mentioned."""
    people = []
    for username in sorted(core.VALID_USERS):
        if not any(user_can(username, p) for p in pages):
            continue
        d = user_details(username)
        people.append({"username": username, "name": _display_name(username), "email": d.get("email") or ""})
    return people


def _mentions(body: str, pages: tuple = (PAGE,)) -> list:
    """Usernames @mentioned in the text that can open the pages, in order, once each."""
    known = {p["username"].lower(): p["username"] for p in _commenters(pages)}
    found = []
    for token in _MENTION_RE.findall(body):
        username = known.get(token.rstrip(".").lower())
        if username and username not in found:
            found.append(username)
    return found


def _settings() -> dict:
    from .kpi import _kpi_settings, _read_kpi_layout_value
    return _kpi_settings(_read_kpi_layout_value('settings'))


def _check_comments_on() -> None:
    if int(_settings().get("wbr_comments_on", 0)) != 1:
        raise ValueError("Comments are turned off (Weekly Business Review > Settings)")


def _check_target(target: str) -> None:
    """The target must be a chart that is on the review (grid or slides)."""
    from .kpi import _read_kpi_layout_values, _WBR_ITEMS_KEY, _WBR_DECK_KEY
    m = _TARGET_RE.match(target or "")
    if not m:
        raise ValueError("Unknown chart")
    ref, period = m.groups()
    stored = _read_kpi_layout_values(['layout', _WBR_ITEMS_KEY, _WBR_DECK_KEY])
    rows = {r.get("id") for r in (stored.get('layout') or {}).get("rows", [])}
    placed = {(i.get("ref"), i.get("period")) for i in stored.get(_WBR_ITEMS_KEY) or []}
    for slide in (stored.get(_WBR_DECK_KEY) or {}).get("slides", []):
        placed.update((i.get("ref"), i.get("period")) for i in slide.get("items", []))
    if ref not in rows or (ref, period) not in placed:
        raise ValueError("That chart is not on the Weekly Business Review")


def _clean_body(body) -> str:
    text = str(body or "").strip()
    if not text:
        raise ValueError("Type a comment first")
    if len(text) > BODY_MAX:
        raise ValueError(f"A comment can be at most {BODY_MAX} characters")
    return text


def _add_alerts(cur, people, kind: str, comment_id: int, root_id: int, from_user: str, body: str,
                link_for=None) -> None:
    """link_for(username) gives that person's link; none = the review page."""
    who = _display_name(from_user)
    verb = "mentioned you" if kind == "mention" else "replied"
    text = f"{who} {verb}: {body}"
    if len(text) > 300:
        text = text[:297] + "..."
    for username in people:
        if username == from_user:
            continue
        link = link_for(username) if link_for else f"/weekly-business-review?comment={root_id}"
        cur.execute(
            "INSERT INTO user_alerts (username, kind, comment_id, from_user, text, link) VALUES (%s, %s, %s, %s, %s, %s)",
            (username, kind, comment_id, from_user, text, link))


def _insert_comment(cur, user: str, payload: dict, body: str, mentioned: list, check_target, link_for=None,
                    prefix=_REVIEW_PREFIXES) -> int:
    """Adds a comment or a reply and its alerts; returns the new id. A reply
    takes its thread's target; a new thread's target goes through check_target.
    Only threads whose target starts with `prefix` can be replied to."""
    parent_id = payload.get("parent_id")
    parent = None
    if parent_id is not None:
        if not isinstance(parent_id, int) or isinstance(parent_id, bool):
            raise ValueError("Bad thread")
        parent = _load(cur, parent_id)
        if parent["parent_id"] is not None:
            raise ValueError("Reply to the thread, not to a reply")
        target = parent["target"]
        if not target.startswith(prefix):
            raise ValueError("Bad thread")
    else:
        target = str(payload.get("target") or "")
        check_target(target)
    assigned = mentioned[0] if (payload.get("assign") and mentioned and parent is None) else None
    cur.execute(
        """INSERT INTO wbr_comments (target, parent_id, author, body, mentions, assigned_to)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (target, parent_id, user, body, mentioned, assigned))
    new_id = cur.fetchone()[0]
    root_id = parent["id"] if parent else new_id
    _add_alerts(cur, mentioned, "mention", new_id, root_id, user, body, link_for)
    if parent:
        # The thread's author and assignee hear about replies,
        # unless the reply already mentions them.
        others = [p for p in (parent["author"], parent["assigned_to"]) if p and p not in mentioned]
        _add_alerts(cur, list(dict.fromkeys(others)), "reply", new_id, root_id, user, body, link_for)
    return new_id


def _row(r) -> dict:
    keys = ("id", "target", "parent_id", "author", "body", "mentions", "assigned_to",
            "resolved_at", "resolved_by", "created_at", "edited_at")
    out = dict(zip(keys, r))
    for k in ("resolved_at", "created_at", "edited_at"):
        out[k] = out[k].isoformat() if out[k] else None
    out["mentions"] = list(out["mentions"] or [])
    out["author_name"] = _display_name(out["author"])
    return out


def _load(cur, comment_id: int):
    cur.execute(
        "SELECT id, target, parent_id, author, assigned_to, deleted_at FROM wbr_comments WHERE id = %s",
        (comment_id,))
    r = cur.fetchone()
    if not r or r[5] is not None:
        raise ValueError("That comment has been deleted")
    return {"id": r[0], "target": r[1], "parent_id": r[2], "author": r[3], "assigned_to": r[4]}


def _error(e: Exception) -> dict:
    return {"status": "error", "message": str(e)}


# ── Comments ─────────────────────────────────────────────────────────────────

@router.get("/weekly-business-review/comments")
def comments_list(request: Request, user: str = Depends(require_page_api(PAGE))):
    """Every comment on the review (open and resolved), and the @ list."""
    try:
        with get_prod_pricer_connection() as conn:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, target, parent_id, author, body, mentions, assigned_to,
                              resolved_at, resolved_by, created_at, edited_at
                       FROM wbr_comments WHERE deleted_at IS NULL AND (target LIKE 'chart:%' OR target LIKE 'table:%')
                       ORDER BY created_at""")
                comments = [_row(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"Weekly Business Review comments load failed: {e}")
        return _error(e)
    return {"status": "ok", "comments": comments, "people": _commenters(), "me": user, "is_admin": is_admin(user)}


@router.post("/weekly-business-review/comments")
def comments_add(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE))):
    """{target, body, parent_id?, assign?}. A reply takes its thread's target."""
    payload = payload or {}
    try:
        _check_comments_on()
        body = _clean_body(payload.get("body"))
        mentioned = _mentions(body)
        with get_prod_pricer_connection() as conn:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                new_id = _insert_comment(cur, user, payload, body, mentioned, _check_target)
            conn.commit()
    except ValueError as e:
        return _error(e)
    except Exception as e:
        logger.error(f"Weekly Business Review comment save failed: {e}")
        return _error(e)
    _track(request, 'wbr_comment', '/weekly-business-review')
    return {"status": "ok", "id": new_id}


# Edit, resolve and delete work on any comment by id: a chart comment (review
# page) or a vehicle comment (the pages in VEHICLE_PAGES). What the user may do
# is checked against the comment's own target.
_ANY_COMMENT_PAGE = require_page_api(PAGE, *VEHICLE_PAGES)


def _check_open_to(user: str, target: str) -> None:
    """The user can open the comment's page, and review comments are switched on."""
    _check_access(user, target)
    if target.startswith(_REVIEW_PREFIXES):
        _check_comments_on()


def _vehicle_link_for(stock_id: str, page: str, vrm=None):
    """link_for(username) for alerts about a vehicle: the posting page when that
    person can open it, else the first of the vehicle pages they can open. The
    page opens the vehicle's notes from ?vehicle= (vehicle_comments.js)."""
    plate = re.sub(r"[^A-Za-z0-9]", "", str(vrm or ""))[:12]
    query = f"?vehicle={stock_id}" + (f"&vrm={plate}" if plate else "")

    def link(username: str) -> str:
        for key in (page, *VEHICLE_PAGES):
            if key in VEHICLE_PAGES and user_can(username, key):
                return core.PAGES_BY_KEY[key].path + query
        return core.PAGES_BY_KEY[VEHICLE_PAGES[0]].path + query
    return link


@router.post("/weekly-business-review/comments/{comment_id}/edit")
@router.post("/api/comments/{comment_id}/edit")
def comments_edit(comment_id: int, request: Request, payload: dict, user: str = Depends(_ANY_COMMENT_PAGE)):
    """The author changes their own comment. Newly mentioned people are alerted."""
    try:
        body = _clean_body((payload or {}).get("body"))
        with get_prod_pricer_connection() as conn:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                c = _load(cur, comment_id)
                _check_open_to(user, c["target"])
                if c["author"] != user:
                    raise ValueError("Only the person who wrote a comment can edit it")
                mentioned = _mentions(body, _pages_for(c["target"]))
                cur.execute("SELECT mentions FROM wbr_comments WHERE id = %s", (comment_id,))
                before = set(cur.fetchone()[0] or [])
                cur.execute("UPDATE wbr_comments SET body = %s, mentions = %s, edited_at = NOW() WHERE id = %s",
                            (body, mentioned, comment_id))
                link_for = None
                if c["target"].startswith('vehicle:'):
                    link_for = _vehicle_link_for(c["target"][len('vehicle:'):], (payload or {}).get("page"),
                                                 (payload or {}).get("vrm"))
                _add_alerts(cur, [m for m in mentioned if m not in before], "mention", comment_id,
                            c["parent_id"] or comment_id, user, body, link_for)
            conn.commit()
    except ValueError as e:
        return _error(e)
    except Exception as e:
        logger.error(f"Weekly Business Review comment edit failed: {e}")
        return _error(e)
    return {"status": "ok"}


@router.post("/weekly-business-review/comments/{comment_id}/resolve")
@router.post("/api/comments/{comment_id}/resolve")
def comments_resolve(comment_id: int, request: Request, payload: dict, user: str = Depends(_ANY_COMMENT_PAGE)):
    """{resolved: true|false}. Threads only; the author, assignee or admin."""
    resolved = bool((payload or {}).get("resolved"))
    try:
        with get_prod_pricer_connection() as conn:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                c = _load(cur, comment_id)
                _check_open_to(user, c["target"])
                if c["parent_id"] is not None:
                    raise ValueError("Resolve the thread, not a reply")
                if user not in (c["author"], c["assigned_to"]) and not is_admin(user):
                    raise ValueError("Only the person who started the thread, the person it is assigned to, or the admin can resolve it")
                cur.execute(
                    "UPDATE wbr_comments SET resolved_at = CASE WHEN %s THEN NOW() END, resolved_by = CASE WHEN %s THEN %s END WHERE id = %s",
                    (resolved, resolved, user, comment_id))
            conn.commit()
    except ValueError as e:
        return _error(e)
    except Exception as e:
        logger.error(f"Weekly Business Review comment resolve failed: {e}")
        return _error(e)
    return {"status": "ok"}


@router.post("/weekly-business-review/comments/{comment_id}/delete")
@router.post("/api/comments/{comment_id}/delete")
def comments_delete(comment_id: int, request: Request, user: str = Depends(_ANY_COMMENT_PAGE)):
    """The author or the admin. Kept in the table, marked deleted; a thread's
    replies go with it, and the alerts about them are removed."""
    try:
        with get_prod_pricer_connection() as conn:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                c = _load(cur, comment_id)
                _check_access(user, c["target"])
                if c["author"] != user and not is_admin(user):
                    raise ValueError("Only the person who wrote a comment, or the admin, can delete it")
                cur.execute(
                    "UPDATE wbr_comments SET deleted_at = NOW() WHERE (id = %s OR parent_id = %s) AND deleted_at IS NULL RETURNING id",
                    (comment_id, comment_id))
                gone = [r[0] for r in cur.fetchall()]
                cur.execute("DELETE FROM user_alerts WHERE comment_id = ANY(%s)", (gone,))
            conn.commit()
    except ValueError as e:
        return _error(e)
    except Exception as e:
        logger.error(f"Weekly Business Review comment delete failed: {e}")
        return _error(e)
    return {"status": "ok"}


# ── Alerts (the bell in the page header) ─────────────────────────────────────

def _signed_in(request: Request) -> str:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    return user


@router.get("/api/alerts")
def alerts_list(request: Request, user: str = Depends(_signed_in)):
    """This user's latest 30 alerts and how many are unread. total = 0 means the
    bell is not shown at all."""
    try:
        with get_prod_pricer_connection() as conn:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, kind, from_user, text, link, created_at, read_at FROM user_alerts
                       WHERE username = %s ORDER BY created_at DESC LIMIT 30""", (user,))
                rows = cur.fetchall()
                cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE read_at IS NULL) FROM user_alerts WHERE username = %s", (user,))
                total, unread = cur.fetchone()
    except Exception as e:
        logger.error(f"Alerts load failed: {e}")
        return _error(e)
    alerts = [{"id": r[0], "kind": r[1], "from_user": r[2], "from_name": _display_name(r[2]) if r[2] else "",
               "text": r[3], "link": r[4], "created_at": r[5].isoformat(), "read": r[6] is not None} for r in rows]
    return {"status": "ok", "total": total, "unread": unread, "alerts": alerts}


@router.post("/api/alerts/read")
def alerts_read(request: Request, payload: dict, user: str = Depends(_signed_in)):
    """{ids: [..]} or {all: true}: mark this user's alerts read."""
    payload = payload or {}
    ids = [i for i in (payload.get("ids") or []) if isinstance(i, int) and not isinstance(i, bool)]
    try:
        with get_prod_pricer_connection() as conn:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                if payload.get("all"):
                    cur.execute("UPDATE user_alerts SET read_at = NOW() WHERE username = %s AND read_at IS NULL", (user,))
                elif ids:
                    cur.execute("UPDATE user_alerts SET read_at = NOW() WHERE username = %s AND id = ANY(%s) AND read_at IS NULL",
                                (user, ids))
            conn.commit()
    except Exception as e:
        logger.error(f"Alerts mark read failed: {e}")
        return _error(e)
    return {"status": "ok"}
