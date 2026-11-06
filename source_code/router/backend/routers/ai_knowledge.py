"""Settings > AI: per-page Ask AI settings (JSON APIs behind
static/js/ai_settings.js). Admin only - every route returns 403 for anyone
else, since the Settings page itself only redirects. The knowledge entries are
edited on the Semantic & Context Layer page (routers/semantic_layer.py)."""

import logging

from fastapi import APIRouter, Request, Depends, HTTPException

from utils import ai_knowledge as ak
from .core import get_current_user, is_admin

logger = logging.getLogger(__name__)

router = APIRouter()


def _admin(user: str) -> None:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")


def usage(entries: list) -> dict:
    """Per page: how much of its knowledge size limit is used, and what is left out."""
    fields = ak.ai_fields(ak.get_reference_settings())
    result = {}
    for page in ak.PAGE_KEYS:
        settings = ak.get_page_settings(page)
        k = ak.assemble_knowledge(entries, page, settings["knowledge_max_chars"], fields)
        result[page] = {"chars": k["chars"], "max_chars": k["max_chars"], "skipped": k["skipped"]}
    return result


@router.get("/settings/ai/usage")
def ai_usage(request: Request, user: str = Depends(get_current_user)):
    _admin(user)
    return {"pages": [list(p) for p in ak.AI_PAGES], "usage": usage(ak.list_entries())}


@router.get("/settings/ai/page/{page}")
def ai_page_get(page: str, request: Request, user: str = Depends(get_current_user)):
    _admin(user)
    if page not in ak.PAGE_KEYS:
        raise HTTPException(status_code=404)
    return {"settings": ak.get_page_settings(page), "defaults": ak.PAGE_DEFAULTS[page],
            "limits": ak.PAGE_LIMITS}


@router.post("/settings/ai/page/{page}")
def ai_page_save(page: str, request: Request, payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        settings = ak.save_page_settings(page, payload or {}, user)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    logger.info("Settings [AI] %s page settings saved by %s", page, user)
    return {"status": "ok", "settings": settings}


@router.post("/settings/ai/page/{page}/reset")
def ai_page_reset(page: str, request: Request, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        settings = ak.reset_page_settings(page)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    logger.info("Settings [AI] %s page settings reset by %s", page, user)
    return {"status": "ok", "settings": settings}


@router.get("/settings/ai/preview/{page}")
def ai_preview(page: str, request: Request, user: str = Depends(get_current_user)):
    """Exactly what the page's Ask AI sends before the question and data."""
    _admin(user)
    if page not in ak.PAGE_KEYS:
        raise HTTPException(status_code=404)
    settings, knowledge, prompt = ak.page_prompt(page)
    return {"prompt": prompt, "chars": len(prompt), "knowledge_chars": knowledge["chars"],
            "knowledge_max_chars": knowledge["max_chars"], "skipped": knowledge["skipped"]}
