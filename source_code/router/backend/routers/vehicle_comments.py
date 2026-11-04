"""Notes on a vehicle, kept for good under its Autoview stock id.

The same threads, replies, @mentions, Assigned to and Resolve as the Weekly
Business Review comments, and the same table (wbr_comments), with the target
'vehicle:<Autoview stock id>'. People see the VRM on every page. The Autoview id
is the key because one car can have several Apex ids (3355 and 00019484), and an
Apex id changes when the car sells (250077 becomes 250077-SOLD). A car with no
Autoview id has no Notes button. Anyone who can open one of the pages in
wbr_comments.VEHICLE_PAGES can read and write them.

Site changes are here too because they show in a vehicle's notes history. They
are made on the Trade Report (routers/trade_report.py) and only change the site
there. Removing one keeps its row, so the history stays complete.

Edit, resolve and delete are in wbr_comments.py (/api/comments/<id>/edit and so
on). The page side is static/js/vehicle_comments.js.
"""
import logging

from fastapi import APIRouter, Depends, Request

from utils.db_utils import get_prod_pricer_connection
from utils.ddl_once import once_per_database
from . import wbr_comments as wc
from .core import _track, is_admin, require_page_api

logger = logging.getLogger(__name__)
router = APIRouter()

_VEHICLE_ACCESS = require_page_api(*wc.VEHICLE_PAGES)


@once_per_database
def ensure_site_changes_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS vehicle_site_changes (
                id         BIGSERIAL    PRIMARY KEY,
                stock_id   VARCHAR(40)  NOT NULL,
                vrm        VARCHAR(20),
                old_site   VARCHAR(100),
                new_site   VARCHAR(100) NOT NULL,
                reason     TEXT         NOT NULL,
                changed_by VARCHAR(100) NOT NULL,
                changed_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                removed_by VARCHAR(100),
                removed_at TIMESTAMPTZ
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS vehicle_site_changes_stock ON vehicle_site_changes (stock_id)")
    conn.commit()


_SITE_COLS = "id, stock_id, vrm, old_site, new_site, reason, changed_by, changed_at, removed_by, removed_at"


def site_change_row(r) -> dict:
    out = dict(zip(("id", "stock_id", "vrm", "old_site", "new_site", "reason",
                    "changed_by", "changed_at", "removed_by", "removed_at"), r))
    out["changed_by_name"] = wc._display_name(out["changed_by"])
    out["removed_by_name"] = wc._display_name(out["removed_by"]) if out["removed_by"] else None
    for k in ("changed_at", "removed_at"):
        out[k] = out[k].isoformat() if out[k] else None
    return out


def read_current_site_changes(conn) -> dict:
    """{stock id: its latest site change that has not been removed}."""
    ensure_site_changes_table(conn)
    with conn.cursor() as cur:
        cur.execute(f"""SELECT DISTINCT ON (stock_id) {_SITE_COLS} FROM vehicle_site_changes
                        WHERE removed_at IS NULL ORDER BY stock_id, changed_at DESC, id DESC""")
        return {r[1]: site_change_row(r) for r in cur.fetchall()}


def _target(stock_id) -> str:
    target = f"vehicle:{str(stock_id or '').strip()}"
    if not wc.VEHICLE_TARGET_RE.match(target):
        raise ValueError("Unknown vehicle")
    return target


@router.get("/api/vehicle-comments")
def vehicle_comments_list(request: Request, stock_id: str, user: str = Depends(_VEHICLE_ACCESS)):
    """Every note on one vehicle, oldest first (open and resolved), its site
    changes, and the @ list."""
    try:
        target = _target(stock_id)
        with get_prod_pricer_connection() as conn:
            wc._ensure_tables(conn)
            ensure_site_changes_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, target, parent_id, author, body, mentions, assigned_to,
                              resolved_at, resolved_by, created_at, edited_at
                       FROM wbr_comments WHERE deleted_at IS NULL AND target = %s ORDER BY created_at, id""",
                    (target,))
                comments = [wc._row(r) for r in cur.fetchall()]
                cur.execute(f"SELECT {_SITE_COLS} FROM vehicle_site_changes WHERE stock_id = %s ORDER BY changed_at, id",
                            (target[len('vehicle:'):],))
                site_changes = [site_change_row(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"Vehicle notes load failed: {e}")
        return wc._error(e)
    for c in comments:
        c["resolved_by_name"] = wc._display_name(c["resolved_by"]) if c["resolved_by"] else None
        c["assigned_to_name"] = wc._display_name(c["assigned_to"]) if c["assigned_to"] else None
    return {"status": "ok", "comments": comments, "site_changes": site_changes,
            "people": wc._commenters(wc.VEHICLE_PAGES), "me": user, "is_admin": is_admin(user)}


@router.get("/api/vehicle-comments/counts")
def vehicle_comments_counts(request: Request, user: str = Depends(_VEHICLE_ACCESS)):
    """{stock id: notes plus site changes} for every vehicle that has any, for
    the button on each row."""
    try:
        with get_prod_pricer_connection() as conn:
            wc._ensure_tables(conn)
            ensure_site_changes_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, SUM(n) FROM (
                           SELECT SUBSTRING(target FROM 9) AS id, COUNT(*) AS n FROM wbr_comments
                           WHERE deleted_at IS NULL AND target LIKE 'vehicle:%' GROUP BY 1
                           UNION ALL
                           SELECT stock_id, COUNT(*) FROM vehicle_site_changes GROUP BY 1
                       ) x GROUP BY id""")
                counts = {r[0]: int(r[1]) for r in cur.fetchall()}
    except Exception as e:
        logger.error(f"Vehicle note counts failed: {e}")
        return wc._error(e)
    return {"status": "ok", "counts": counts}


@router.post("/api/vehicle-comments")
def vehicle_comments_add(request: Request, payload: dict, user: str = Depends(_VEHICLE_ACCESS)):
    """{stock_id, body, vrm?, page?, parent_id?, assign?}. A reply takes its thread's vehicle."""
    payload = payload or {}
    try:
        target = _target(payload.get("stock_id"))
        body = wc._clean_body(payload.get("body"))
        mentioned = wc._mentions(body, wc.VEHICLE_PAGES)
        link_for = wc._vehicle_link_for(target[len('vehicle:'):], payload.get("page"), payload.get("vrm"))
        with get_prod_pricer_connection() as conn:
            wc._ensure_tables(conn)
            with conn.cursor() as cur:
                new_id = wc._insert_comment(cur, user, {**payload, "target": target}, body, mentioned,
                                            lambda t: None, link_for, prefix='vehicle:')
            conn.commit()
    except ValueError as e:
        return wc._error(e)
    except Exception as e:
        logger.error(f"Vehicle note save failed: {e}")
        return wc._error(e)
    _track(request, 'vehicle_comment', '/api/vehicle-comments')
    return {"status": "ok", "id": new_id}
