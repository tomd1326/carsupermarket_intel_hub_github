"""Settings > GA4 Requests: the list of GA4 API requests and their Snowflake
tables, admin only. See utils/ga4_requests.py."""
import logging

from fastapi import APIRouter, Depends, HTTPException

from utils import ga4_requests as g4
from utils.snowflake_client import SnowflakeClient

from .core import get_current_user, is_admin
from . import kpi

logger = logging.getLogger(__name__)

router = APIRouter()

_KEY = 'ga4_requests'


def _admin(user):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")


def _saved() -> list:
    """The saved list; the Rundeck steps of 2026-10-23 until it is first saved."""
    stored = kpi._read_kpi_layout_value(_KEY)
    rows = stored if isinstance(stored, list) else g4.SEED
    return [g4.with_defaults(r) for r in rows if isinstance(r, dict)]


def _listing(rows: list) -> list:
    return [{**r, **g4.derived(r)} for r in rows]


@router.get("/api/ga4-requests")
def ga4_requests_list(user: str = Depends(get_current_user)):
    _admin(user)
    try:
        return {"status": "ok", "requests": _listing(_saved())}
    except Exception as e:
        logger.error("GA4 requests read failed: %s", e)
        return {"status": "error", "message": str(e)}


@router.post("/api/ga4-requests")
def ga4_requests_save(payload: dict, user: str = Depends(get_current_user)):
    """Add a request, or replace the one named `original` (its old query name)."""
    _admin(user)
    payload = payload or {}
    req, original = payload.get("request"), payload.get("original")
    try:
        rows = _saved()
        others = [r for r in rows if r["query_name"] != original]
        err = g4.validate(req, others)
        if err:
            return {"status": "error", "message": err}
        req = g4.clean(req)
        if original and any(r["query_name"] == original for r in rows):
            rows = [req if r["query_name"] == original else r for r in rows]
        else:
            if len(rows) >= g4.REQUESTS_MAX:
                return {"status": "error", "message": f"At most {g4.REQUESTS_MAX} requests"}
            rows.append(req)
        kpi._write_kpi_layout_value(_KEY, rows)
        logger.info("GA4 request %s saved by %s", req["query_name"], user)
    except Exception as e:
        logger.error("GA4 request save failed: %s", e)
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "requests": _listing(rows)}


@router.delete("/api/ga4-requests/{query_name}")
def ga4_requests_delete(query_name: str, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        rows = [r for r in _saved() if r["query_name"] != query_name]
        kpi._write_kpi_layout_value(_KEY, rows)
        logger.info("GA4 request %s deleted by %s", query_name, user)
    except Exception as e:
        logger.error("GA4 request delete failed: %s", e)
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "requests": _listing(rows)}


@router.post("/api/ga4-requests/move")
def ga4_requests_move(payload: dict, user: str = Depends(get_current_user)):
    """Move one request up (-1) or down (1) the list."""
    _admin(user)
    query_name, step = (payload or {}).get("query_name"), (payload or {}).get("step")
    try:
        rows = _saved()
        i = next((n for n, r in enumerate(rows) if r["query_name"] == query_name), None)
        if i is None or step not in (-1, 1) or not 0 <= i + step < len(rows):
            return {"status": "error", "message": "Cannot move that request"}
        rows[i], rows[i + step] = rows[i + step], rows[i]
        kpi._write_kpi_layout_value(_KEY, rows)
    except Exception as e:
        logger.error("GA4 request move failed: %s", e)
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "requests": _listing(rows)}


@router.get("/api/ga4-requests/snowflake")
def ga4_requests_snowflake(user: str = Depends(get_current_user)):
    """For every request's table in PROD: whether it exists, when it was last
    changed, its row count and its latest date. One INFORMATION_SCHEMA read,
    then one query over the tables that exist."""
    _admin(user)
    try:
        wanted = {g4.derived(r)["table"].split(".")[1]: r["query_name"] for r in _saved()}
        with SnowflakeClient() as sf:
            rows, _ = sf.execute(f"""
                SELECT table_name, last_altered, row_count
                FROM {kpi._VEHICLE_DB}.INFORMATION_SCHEMA.TABLES
                WHERE table_schema = %s AND table_name LIKE %s ESCAPE '!'
            """, [g4.SCHEMA, 'GA4!_%'])
            found = {str(r[0]).upper(): r for r in rows if str(r[0]).upper() in wanted}
            latest = {}
            if found:
                # Names come from INFORMATION_SCHEMA, so they are real tables.
                union = " UNION ALL ".join(
                    f"SELECT '{t}' AS T, MAX(date)::DATE AS D FROM {kpi._VEHICLE_DB}.{g4.SCHEMA}.{t}" for t in found)
                try:
                    latest = {str(r[0]): r[1] for r in sf.execute(union)[0]}
                except Exception as e:
                    logger.warning("GA4 latest dates read failed: %s", e)
        out = {}
        for table, query_name in wanted.items():
            r = found.get(table)
            out[query_name] = ({"exists": True, "last_altered": kpi._json_cell(r[1]), "row_count": r[2],
                                "latest_date": kpi._json_cell(latest.get(table))} if r else {"exists": False})
        return {"status": "ok", "tables": out}
    except Exception as e:
        logger.error("GA4 Snowflake check failed: %s", e)
        return {"status": "error", "message": str(e)}
