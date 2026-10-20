"""SharePoint files: the download worker, page status, and Settings > SharePoint.

The reading itself is in utils/sharepoint_sources.py.
"""
import logging

from fastapi import APIRouter, Request, Depends, HTTPException

from utils import sharepoint_sources as sps

from .core import get_current_user, is_admin, check_worker_secret, require_page_api

logger = logging.getLogger(__name__)

router = APIRouter()


def _admin(user):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/api/sharepoint/refresh-run")
def sharepoint_refresh_run(data: dict):
    """Internal worker route, invoked by our own Lambda (local runs use a
    thread instead, see sharepoint_sources._dispatch). Only downloads: it never
    checks and dispatches, so a worker can never start another worker."""
    check_worker_secret(data.get("secret"))
    job_id = str(data.get("job_id") or "")
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id missing")
    sps.run_job(job_id)
    return {"ok": True}


@router.get("/api/sharepoint/status")
def sharepoint_status(keys: str = "", check: bool = False, user: str = Depends(require_page_api('kpi'))):
    """Status of the given files (comma separated keys) for an open page.
    check=true first runs the normal check for any that are due, which starts
    a download when a file has been saved since its copy. The KPI page is the
    only caller, so only people who can open it may ask."""
    wanted = [k for k in (keys or "").split(",") if k]
    try:
        if check and wanted:
            sps.check_due(wanted)
        rows = sps.status(wanted or None)
    except Exception as e:
        logger.error("SharePoint status failed: %s", e)
        return {"status": "error", "message": str(e)}
    keep = ("source_key", "name", "enabled", "check_minutes", "last_checked_at", "last_saved_at",
            "copy_saved_at", "last_downloaded_at", "last_error", "last_error_at", "job")
    return {"status": "ok", "files": [{k: r.get(k) for k in keep} for r in rows]}


# ── Settings > SharePoint ────────────────────────────────────────────────────

@router.get("/api/sharepoint/sources")
def sharepoint_sources_list(user: str = Depends(get_current_user)):
    _admin(user)
    try:
        rows = sps.status()
        jobs = sps.recent_jobs([r["source_key"] for r in rows], per_file=8)
    except Exception as e:
        logger.error("SharePoint file list failed: %s", e)
        return {"status": "error", "message": str(e)}
    for r in rows:
        r["recent_jobs"] = [sps._serialise(j) for j in jobs.get(r["source_key"], [])]
        r["fixed"] = r["source_key"] in sps.FIXED_KEYS
        r["default_headers"] = sps.DEFAULT_HEADERS.get(r["file_type"])
    return {
        "status": "ok",
        "files": rows,
        "file_types": sps.FILE_TYPES,
        "required_headers": list(sps.REQUIRED_HEADERS),
    }


@router.post("/api/sharepoint/sources")
def sharepoint_sources_save(payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        key = sps.save_source(payload or {}, user)
    except (ValueError, KeyError) as e:
        return {"status": "error", "message": str(e).strip("'\"")}
    except Exception as e:
        logger.exception("SharePoint file save failed")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "source_key": key}


@router.delete("/api/sharepoint/sources/{key}")
def sharepoint_sources_delete(key: str, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        sps.delete_source(key, user)
    except (ValueError, KeyError) as e:
        return {"status": "error", "message": str(e).strip("'\"")}
    except Exception as e:
        logger.exception("SharePoint file delete failed")
        return {"status": "error", "message": str(e)}
    return {"status": "ok"}


@router.post("/api/sharepoint/sources/{key}/check")
def sharepoint_sources_check(key: str, user: str = Depends(get_current_user)):
    """Check Now: ask SharePoint when the file was saved, download only if
    saved since the copy."""
    _admin(user)
    try:
        return {"status": "ok", **sps.request_refresh(key, user)}
    except Exception as e:
        return {"status": "error", "message": str(e).strip("'\"")}


@router.post("/api/sharepoint/sources/{key}/reread")
def sharepoint_sources_reread(key: str, user: str = Depends(get_current_user)):
    """Re-read Now: download the file whether or not it has changed."""
    _admin(user)
    try:
        return {"status": "ok", **sps.request_refresh(key, user, reread=True)}
    except Exception as e:
        return {"status": "error", "message": str(e).strip("'\"")}


@router.post("/api/sharepoint/test")
def sharepoint_test(payload: dict, user: str = Depends(get_current_user)):
    """Test Access: reaches the file and lists its sheets and tables, but
    reads no cells."""
    _admin(user)
    payload = payload or {}
    try:
        return {"status": "ok", **sps.test_access(payload.get("url"), payload.get("read_kind") or "sheet",
                                                  payload.get("sheet_or_table") or "")}
    except Exception as e:
        logger.exception("SharePoint test access failed")
        return {"status": "error", "message": str(e)}
