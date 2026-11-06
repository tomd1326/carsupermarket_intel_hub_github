"""The Semantic & Context Layer page (/semantic-layer): metrics, dimensions,
tables and columns (semantic layer), and glossary terms, business rules and AI
instructions (context layer). Anyone with the page can read it and download it
as YAML or JSON; only the admin can add, edit, approve or delete entries and
change the page settings. The entries are the same ones every Ask AI reads
(utils/ai_knowledge.py, table ai_knowledge). JSON APIs behind
static/js/semantic_layer.js."""

import json
import logging

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response

from utils import ai_knowledge as ak
from .core import BASE_URL, templates, is_admin, page_name, require_page, require_page_api, _track, _uk_now
from .kpi import _source_tables, _source_columns, _allowed_source_table
from .ai_knowledge import usage

logger = logging.getLogger(__name__)

router = APIRouter()

PAGE_KEY = "semantic_layer"


def _admin(user: str) -> None:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")


def _visible(entries: list, settings: dict, user: str) -> list:
    """The admin sees every entry, proposed and hidden ones included. Everyone
    else sees active entries of the kinds shown that are not hidden."""
    if is_admin(user):
        return entries
    return [e for e in entries if e["status"] == "active" and e.get("show_on_page", True)
            and e["kind"] in settings["kinds_shown"]]


@router.get("/semantic-layer", response_class=HTMLResponse)
def semantic_layer_page(request: Request, user: str = Depends(require_page(PAGE_KEY))):
    _track(request, 'page_view', '/semantic-layer')
    return templates.TemplateResponse("semantic_layer.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL, "show_admin": is_admin(user),
    })


@router.get("/semantic-layer/data")
def semantic_layer_data(request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    settings = ak.get_reference_settings()
    entries = ak.list_entries()
    data = {
        "entries": _visible(entries, settings, user),
        "settings": settings,
        "kinds": ak.KINDS,
        "layers": ak.LAYERS,
        "is_admin": is_admin(user),
    }
    if is_admin(user):
        data["pages"] = [["all", "All Pages"]] + [list(p) for p in ak.AI_PAGES] + [["self_analytics", "Self-Analytics"]]
        data["field_types"] = ak.FIELD_TYPES
        data["usage"] = usage(entries)
    return data


# ── Admin: entries ───────────────────────────────────────────────────────────

@router.post("/semantic-layer/save")
def semantic_layer_save(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    _admin(user)
    try:
        entry = ak.save_entry(payload or {}, user)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    # A category typed in the editor that is not in the list yet is added to it.
    category = entry.get("category")
    if category:
        settings = ak.get_reference_settings()
        if category.lower() not in (c.lower() for c in settings["categories"]):
            ak.save_reference_settings({**settings, "categories": settings["categories"] + [category]}, user)
    logger.info("Semantic layer entry '%s' saved by %s", entry["title"], user)
    return {"status": "ok", "entry": entry}


@router.post("/semantic-layer/delete")
def semantic_layer_delete(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    _admin(user)
    ak.delete_entry(str((payload or {}).get("id") or ""))
    logger.info("Semantic layer entry %s deleted by %s", (payload or {}).get("id"), user)
    return {"status": "ok"}


@router.post("/semantic-layer/status")
def semantic_layer_status(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    """Approve (status active) or send back to proposed."""
    _admin(user)
    try:
        ak.set_status(str((payload or {}).get("id") or ""), (payload or {}).get("status"), user)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    return {"status": "ok"}


@router.post("/semantic-layer/approve-all")
def semantic_layer_approve_all(request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    _admin(user)
    count = ak.approve_all(user)
    logger.info("Semantic layer: %s proposed entries approved by %s", count, user)
    return {"status": "ok", "approved": count}


@router.post("/semantic-layer/starting-entries")
def semantic_layer_starting_entries(request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    _admin(user)
    return {"status": "ok", "added": ak.add_starting_entries(user)}


# ── Admin: page settings ─────────────────────────────────────────────────────

@router.post("/semantic-layer/settings")
def semantic_layer_settings_save(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    _admin(user)
    settings = ak.save_reference_settings(payload or {}, user)
    logger.info("Semantic layer page settings saved by %s", user)
    return {"status": "ok", "settings": settings}


@router.post("/semantic-layer/settings/reset")
def semantic_layer_settings_reset(request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    _admin(user)
    settings = ak.reset_reference_settings()
    logger.info("Semantic layer page settings reset by %s", user)
    return {"status": "ok", "settings": settings}


# ── Admin: Snowflake table and column pickers ────────────────────────────────

@router.get("/semantic-layer/tables")
def semantic_layer_tables(request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    """COMPUTE_ and PRODUCT_ tables. Takes a few seconds, so read only when asked."""
    _admin(user)
    try:
        return {"status": "ok", "tables": _source_tables()}
    except Exception as e:
        logger.error("Semantic layer table list failed: %s", e)
        return {"status": "error", "message": str(e)}


@router.get("/semantic-layer/columns")
def semantic_layer_columns(table: str, request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    _admin(user)
    if not _allowed_source_table(table):
        return {"status": "error", "message": "Only COMPUTE_ and PRODUCT_ tables"}
    try:
        return {"status": "ok", "columns": _source_columns(table)}
    except Exception as e:
        logger.error("Semantic layer column list for %s failed: %s", table, e)
        return {"status": "error", "message": str(e)}


# ── Download ─────────────────────────────────────────────────────────────────

@router.get("/semantic-layer/export.{fmt}")
def semantic_layer_export(fmt: str, request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    """Active entries the person can see, as YAML or JSON, for analytics tools."""
    if fmt not in ("yaml", "json"):
        raise HTTPException(status_code=404)
    settings = ak.get_reference_settings()
    entries = [e for e in _visible(ak.list_entries(), settings, user) if e["status"] == "active"]
    now = _uk_now()
    data = ak.build_export(entries, settings, now.strftime("%Y-%m-%d %H:%M"), page_name(PAGE_KEY))
    filename = f"{settings['export_name']}_semantic_layer_{now.strftime('%Y%m%d')}.{fmt}"
    if fmt == "yaml":
        body, media = ak.to_yaml(data) + "\n", "application/x-yaml"
    else:
        body, media = json.dumps(data, indent=2, ensure_ascii=False), "application/json"
    _track(request, 'download', f'/semantic-layer/export.{fmt}')
    return Response(content=body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
