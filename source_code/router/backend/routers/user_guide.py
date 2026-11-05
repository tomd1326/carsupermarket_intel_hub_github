"""The User Guide page (/user-guide, sections down the left, each person sees only
what they may) and Settings > User Guide, where the admin writes the sections and
sets who sees them (JSON APIs behind static/js/user_guide_settings.js)."""

import logging
import time

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from utils import user_guide as ug
from utils.db_utils import get_database_connection, read_inactive_users
from .core import (BASE_URL, VALID_USERS, templates, get_current_user, is_admin, page_name, user_can, user_details,
                   visible_pages, _track, PageRedirect)
from .page_registry import PAGES, PAGES_BY_KEY, ALL_PAGE_KEYS
from .settings import SETTINGS_SECTIONS

logger = logging.getLogger(__name__)

router = APIRouter()


def _admin(user: str) -> None:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")


# ── The account menu link ────────────────────────────────────────────────────
# Every page draws the account menu, so what it needs is cached per process for a
# minute, the same as page names. A failed read keeps the last copy; with no copy
# yet, only the admin gets the link.
_MENU_TTL_SECONDS = 60
_menu_sections: list = []
_menu_title: str = ug.SETTINGS_DEFAULTS["title"]
_menu_loaded_at: float = 0.0


def _reload_menu() -> None:
    global _menu_sections, _menu_title, _menu_loaded_at
    _menu_loaded_at = time.time()
    try:
        sections, settings = ug.read_guide()
        _menu_sections = sections
        _menu_title = settings["title"]
    except Exception as e:
        logger.warning("User Guide: could not load the menu details: %s", e)


def user_guide_menu(user: str | None) -> dict:
    """Usage in a template:  {% set guide = user_guide_menu(user_name) %}"""
    if time.time() - _menu_loaded_at >= _MENU_TTL_SECONDS:
        _reload_menu()
    show = is_admin(user) or any(ug.can_see(s, user, is_admin, user_can) for s in _menu_sections)
    return {"show": show, "title": _menu_title}


templates.env.globals['user_guide_menu'] = user_guide_menu


# ── The guide ────────────────────────────────────────────────────────────────

@router.get("/user-guide", response_class=HTMLResponse)
def user_guide_page(request: Request, section: str = "", user: str = Depends(get_current_user)):
    if not user:
        raise PageRedirect(BASE_URL + "/login")
    _track(request, 'page_view', '/user-guide')
    sections, settings, error = [], dict(ug.SETTINGS_DEFAULTS), None
    try:
        all_sections, settings = ug.read_guide()
        sections = [s for s in all_sections if ug.can_see(s, user, is_admin, user_can)]
    except Exception as e:
        logger.warning("User Guide: could not load: %s", e)
        error = str(getattr(e, "detail", None) or e)
    # A section this person may not see is treated as if it did not exist.
    current = next((s for s in sections if str(s["id"]) == section), sections[0] if sections else None)
    page = None
    if current and current["page_key"] in PAGES_BY_KEY and user_can(user, current["page_key"]):
        page = {"name": page_name(current["page_key"]), "url": BASE_URL + PAGES_BY_KEY[current["page_key"]].path}
    return templates.TemplateResponse("user_guide.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL, "error": error,
        "sections": sections, "current": current, "settings": settings, "open_page": page,
        "body_html": ug.render_body(current["body"], BASE_URL) if current else "",
        "visibilities": ug.VISIBILITIES, "show_admin": is_admin(user),
    })


# ── Settings > User Guide ────────────────────────────────────────────────────

def _people() -> list:
    """Everyone who can sign in except the admin, as {'username', 'name'}, by name.
    Accounts not activated yet are left out, as they cannot read anything."""
    try:
        with get_database_connection() as conn:
            inactive = read_inactive_users(conn)
    except Exception as e:
        logger.warning("User Guide: could not read inactive users: %s", e)
        inactive = set()
    people = []
    for username in VALID_USERS:
        if is_admin(username) or username in inactive:
            continue
        details = user_details(username)
        name = f"{details['first_name']} {details['last_name']}".strip()
        people.append({"username": username, "name": name or username})
    return sorted(people, key=lambda p: p["name"].lower())


def _with_audience(section: dict, people: list) -> dict:
    """The section plus the names of who can see it, for the list on the page."""
    return {**section, "audience": [p["name"] for p in people if ug.can_see(section, p["username"], is_admin, user_can)]}


@router.get("/settings/user-guide", response_class=HTMLResponse)
def user_guide_settings_page(request: Request, user: str = Depends(get_current_user)):
    if not is_admin(user):
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    return templates.TemplateResponse("user_guide_settings.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "sections": SETTINGS_SECTIONS, "active_section": "user_guide",
    })


@router.get("/settings/user-guide/data")
def user_guide_data(request: Request, user: str = Depends(get_current_user)):
    _admin(user)
    sections, settings = ug.read_guide()
    people = _people()
    return {
        "sections": [_with_audience(s, people) for s in sections],
        "settings": settings,
        "people": people,
        "pages": [{"key": p.key, "name": page_name(p.key)} for p in PAGES],
        "visibilities": ug.VISIBILITIES,
    }


@router.post("/settings/user-guide/save")
def user_guide_save(request: Request, payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        section = ug.save_section(payload or {}, user, ALL_PAGE_KEYS, set(VALID_USERS))
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    _reload_menu()
    logger.info("User Guide section '%s' saved by %s", section["title"], user)
    return {"status": "ok", "section": section}


@router.post("/settings/user-guide/delete")
def user_guide_delete(request: Request, payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        section_id = int((payload or {}).get("id"))
    except (TypeError, ValueError):
        return {"status": "error", "message": "No section given."}
    ug.delete_section(section_id)
    _reload_menu()
    logger.info("User Guide section %s deleted by %s", section_id, user)
    return {"status": "ok"}


@router.post("/settings/user-guide/reorder")
def user_guide_reorder(request: Request, payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        ids = [int(i) for i in (payload or {}).get("ids") or []]
    except (TypeError, ValueError):
        return {"status": "error", "message": "The order could not be read. Reload the page."}
    ug.reorder_sections(ids)
    return {"status": "ok"}


@router.post("/settings/user-guide/generate")
def user_guide_generate(request: Request, payload: dict | None = None, user: str = Depends(get_current_user)):
    """A starter section for one page (page_key given), or for every page in use
    that has none. All start as Only Me. A page that has a section is left alone."""
    _admin(user)
    page_key = str((payload or {}).get("page_key") or "")
    if page_key:
        if page_key not in ALL_PAGE_KEYS:
            return {"status": "error", "message": "That page does not exist."}
        pages = [(page_key, page_name(page_key))]
    else:
        pages = [(p.key, page_name(p.key)) for p in visible_pages()]
    added = ug.generate_sections(pages, user)
    _reload_menu()
    logger.info("User Guide: %s sections generated by %s", len(added), user)
    return {"status": "ok", "added": added}


@router.post("/settings/user-guide/settings")
def user_guide_settings_save(request: Request, payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        settings = ug.save_settings(payload or {}, user)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    _reload_menu()
    logger.info("User Guide settings saved by %s", user)
    return {"status": "ok", "settings": settings}


@router.post("/settings/user-guide/preview")
def user_guide_preview(request: Request, payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    return {"status": "ok", "html": str(ug.render_body((payload or {}).get("body"), BASE_URL))}
