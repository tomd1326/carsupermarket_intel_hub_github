"""Settings > AI: knowledge entries and per-page Ask AI settings (JSON APIs
behind static/js/ai_settings.js). Admin only - every route returns 403 for
anyone else, since the Settings page itself only redirects."""

import logging

from fastapi import APIRouter, Request, Depends, HTTPException

from utils import ai_knowledge as ak
from utils.cortex import cortex_complete
from .core import get_current_user, is_admin
from .kpi import _source_tables, _source_columns, _allowed_source_table

logger = logging.getLogger(__name__)

router = APIRouter()


def _admin(user: str) -> None:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")


def _usage(entries: list) -> dict:
    """Per page: how much of its knowledge size limit is used, and what is left out."""
    usage = {}
    for page in ak.PAGE_KEYS:
        settings = ak.get_page_settings(page)
        k = ak.assemble_knowledge(entries, page, settings["knowledge_max_chars"])
        usage[page] = {"chars": k["chars"], "max_chars": k["max_chars"], "skipped": k["skipped"]}
    return usage


@router.get("/settings/ai/knowledge")
def ai_knowledge_list(request: Request, user: str = Depends(get_current_user)):
    _admin(user)
    entries = ak.list_entries()
    return {
        "entries": entries,
        "kinds": ak.KINDS,
        "pages": [["all", "All Pages"]] + [list(p) for p in ak.AI_PAGES],
        "usage": _usage(entries),
    }


@router.post("/settings/ai/knowledge/save")
def ai_knowledge_save(request: Request, payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        entry = ak.save_entry(payload or {}, user)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    logger.info("Settings [AI] knowledge entry '%s' saved by %s", entry["title"], user)
    return {"status": "ok", "entry": entry}


@router.post("/settings/ai/knowledge/delete")
def ai_knowledge_delete(request: Request, payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    ak.delete_entry(str((payload or {}).get("id") or ""))
    logger.info("Settings [AI] knowledge entry %s deleted by %s", (payload or {}).get("id"), user)
    return {"status": "ok"}


@router.post("/settings/ai/knowledge/status")
def ai_knowledge_status(request: Request, payload: dict, user: str = Depends(get_current_user)):
    """Approve (status active) or send back to proposed."""
    _admin(user)
    try:
        ak.set_status(str((payload or {}).get("id") or ""), (payload or {}).get("status"), user)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    return {"status": "ok"}


@router.post("/settings/ai/knowledge/starting-entries")
def ai_knowledge_starting_entries(request: Request, user: str = Depends(get_current_user)):
    _admin(user)
    added = ak.add_starting_entries(user)
    return {"status": "ok", "added": added}


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


@router.get("/settings/ai/tables")
def ai_tables(request: Request, user: str = Depends(get_current_user)):
    """Tables a Column Note or Draft With AI can be made for (COMPUTE_ and
    PRODUCT_ schemas). Takes a few seconds, so read only when asked."""
    _admin(user)
    try:
        return {"status": "ok", "tables": _source_tables()}
    except Exception as e:
        logger.error("Settings [AI] table list failed: %s", e)
        return {"status": "error", "message": str(e)}


@router.get("/settings/ai/columns")
def ai_columns(table: str, request: Request, user: str = Depends(get_current_user)):
    _admin(user)
    if not _allowed_source_table(table):
        return {"status": "error", "message": "Only COMPUTE_ and PRODUCT_ tables"}
    try:
        return {"status": "ok", "columns": _source_columns(table)}
    except Exception as e:
        logger.error("Settings [AI] column list for %s failed: %s", table, e)
        return {"status": "error", "message": str(e)}


_DRAFT_PROMPT = """You are writing reference notes that another AI assistant will read to understand a Snowflake table before answering business questions about it. The business is Carsupermarket, a UK used-car retailer.

Table: {table}

Columns (name: type):
{columns}
{notes}
Write plain text, no preamble:
- First line: what one row of this table most likely is.
- Then the columns grouped under short headings (dates, status, vehicle, prices, margin, and so on), each with a few words on what it most likely means.
- Mark anything you are guessing from the name alone with "(guess)". Do not invent values a column holds.
- Keep it compact: group columns that follow one pattern into a single line."""


@router.post("/settings/ai/knowledge/draft")
def ai_knowledge_draft(request: Request, payload: dict, user: str = Depends(get_current_user)):
    """Cortex drafts a Table Description from the table's columns (and any
    Column Notes already written for it). Saved as proposed for Tom to edit
    and approve."""
    _admin(user)
    table = (payload or {}).get("table")
    if not _allowed_source_table(table):
        return {"status": "error", "message": "Only COMPUTE_ and PRODUCT_ tables"}
    try:
        columns = _source_columns(table)
        if not columns:
            return {"status": "error", "message": f"{table} not found"}
        notes = [e for e in ak.list_entries()
                 if e["kind"] == "column" and e["title"].rsplit(".", 1)[0] == table]
        notes_text = ""
        if notes:
            notes_text = "\nNotes already written for some columns (treat these as correct):\n" + "\n".join(
                f"- {e['title'].rsplit('.', 1)[1]}: {e['content'].strip()}" for e in notes) + "\n"
        prompt = _DRAFT_PROMPT.format(
            table=table, notes=notes_text,
            columns="\n".join(f"{name}: {dtype}" for name, dtype in columns.items()))
        data = cortex_complete([{"role": "user", "content": prompt}], max_tokens=4096, temperature=0.2)
        text = data["choices"][0]["messages"].strip()
        if not text:
            return {"status": "error", "message": "Cortex returned an empty draft"}
        entry = ak.propose_entry(table, f"Table PROD_CARSUPERMARKET_DB.{table}. Drafted by AI - check it.\n\n{text}",
                                 "table", "all", user)
    except Exception as e:
        logger.error("Settings [AI] draft for %s failed: %s", table, e)
        return {"status": "error", "message": str(e)}
    logger.info("Settings [AI] draft for %s made by %s", table, user)
    return {"status": "ok", "entry": entry}
