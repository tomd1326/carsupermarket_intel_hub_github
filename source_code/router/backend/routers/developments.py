"""Settings > Developments: the log of what has changed on the site and the update
emails built from it (JSON APIs behind static/js/developments.js, admin only), and
What's New, the read-only page of live developments for anyone signed in. Settings
is admin-only, so What's New is where the people who get the email can read more."""

import logging

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from utils import developments as dv
from utils import email_client
from .core import BASE_URL, templates, get_current_user, is_admin, page_name, user_can, _track, PageRedirect
from .page_registry import PAGES, ALL_PAGE_KEYS
from .settings import SETTINGS_SECTIONS

logger = logging.getLogger(__name__)

router = APIRouter()


def _admin(user: str) -> None:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")


def _pages() -> dict:
    return {p.key: {"name": page_name(p.key), "path": p.path} for p in PAGES}


def _ids(payload: dict) -> list:
    try:
        return [int(i) for i in (payload or {}).get("ids") or []]
    except (TypeError, ValueError):
        raise ValueError("The ticked developments could not be read. Reload the page.")


@router.get("/settings/developments", response_class=HTMLResponse)
def developments_page(request: Request, user: str = Depends(get_current_user)):
    if not is_admin(user):
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    return templates.TemplateResponse("developments.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "sections": SETTINGS_SECTIONS, "active_section": "developments",
    })


@router.get("/settings/developments/data")
def developments_data(request: Request, user: str = Depends(get_current_user)):
    _admin(user)
    developments = dv.list_developments()
    template = dv.get_template()
    return {
        "developments": developments,
        "template": template,
        "types": dv.type_list(template, developments),
        "pages": [{"key": key, "name": page["name"]} for key, page in _pages().items()],
        "emails": dv.list_emails(),
        "sender": email_client.configured_sender(),
    }


@router.post("/settings/developments/save")
def developments_save(request: Request, payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        development = dv.save_development(payload or {}, user, ALL_PAGE_KEYS)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    logger.info("Development '%s' saved by %s", development["title"], user)
    return {"status": "ok", "development": development}


@router.post("/settings/developments/delete")
def developments_delete(request: Request, payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        dev_id = int((payload or {}).get("id"))
    except (TypeError, ValueError):
        return {"status": "error", "message": "No development given."}
    dv.delete_development(dev_id)
    logger.info("Development %s deleted by %s", dev_id, user)
    return {"status": "ok"}


@router.post("/settings/developments/template")
def developments_template_save(request: Request, payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        template = dv.save_template(payload or {}, user)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    logger.info("Development email template saved by %s", user)
    return {"status": "ok", "template": template}


def _build(payload: dict) -> tuple:
    """(subject, intro, ticked developments, ids, built email) for a preview or a generate."""
    payload = payload or {}
    subject = dv.clean_subject(payload.get("subject"))
    intro = str(payload.get("intro") or "").replace("\r\n", "\n").strip()
    ids = _ids(payload)
    developments = dv.get_developments(ids)
    built = dv.build_email(developments, subject, intro, dv.get_template(), _pages())
    return subject, intro, developments, ids, built


@router.post("/settings/developments/email/preview")
def developments_email_preview(request: Request, payload: dict, user: str = Depends(get_current_user)):
    """Drafts can be previewed. Only generating refuses them."""
    _admin(user)
    try:
        _, _, developments, ids, built = _build(payload)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "html": built["html"], "problem": dv.email_problem(ids, developments)}


@router.post("/settings/developments/email/generate")
def developments_email_generate(request: Request, payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        subject, intro, developments, ids, built = _build(payload)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    problem = dv.email_problem(ids, developments)
    if problem:
        return {"status": "error", "message": problem}
    email_id = dv.record_email(subject, intro, developments, built, user)
    logger.info("Development email %s ('%s') generated by %s", email_id, subject, user)
    return {"status": "ok", "id": email_id, "html": built["html"], "text": built["text"]}


@router.get("/settings/developments/email/{email_id}")
def developments_email_get(email_id: int, request: Request, user: str = Depends(get_current_user)):
    _admin(user)
    stored = dv.get_email(email_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="That email no longer exists.")
    return stored


def _send(subject: str, html: str, recipients: list, user: str) -> dict:
    """Everyone goes on BCC, so nobody sees the list or can reply to all of it."""
    sender = email_client.configured_sender()
    try:
        email_client.send_email(sender, subject, html, sender=sender, bcc=recipients)
    except Exception as e:
        logger.exception("Development email '%s' could not be sent", subject)
        return {"status": "error", "message": f"Not sent: {str(e)[:300]}"}
    logger.info("Development email '%s' sent to %s people by %s", subject, len(recipients), user)
    return {"status": "ok", "message": f"Sent from {sender} to {len(recipients)} people."}


@router.post("/settings/developments/email/send")
def developments_email_send(request: Request, payload: dict, user: str = Depends(get_current_user)):
    _admin(user)
    try:
        recipients = dv.clean_recipients((payload or {}).get("to"))
        subject, intro, developments, ids, built = _build(payload)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    problem = dv.email_problem(ids, developments)
    if problem:
        return {"status": "error", "message": problem}
    dv.record_email(subject, intro, developments, built, user)
    return _send(subject, built["html"], recipients, user)


@router.post("/settings/developments/email/{email_id}/send")
def developments_email_resend(email_id: int, request: Request, payload: dict, user: str = Depends(get_current_user)):
    """Send a past email again, exactly as it was kept."""
    _admin(user)
    try:
        recipients = dv.clean_recipients((payload or {}).get("to"))
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    stored = dv.get_email(email_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="That email no longer exists.")
    return _send(stored["subject"], stored["html"], recipients, user)


@router.get("/whats-new", response_class=HTMLResponse)
def whats_new(request: Request, user: str = Depends(get_current_user)):
    if not user:
        raise PageRedirect(BASE_URL + "/login")
    _track(request, 'page_view', '/whats-new')
    pages = _pages()
    error = None
    developments, template = [], dv.TEMPLATE_DEFAULTS
    try:
        developments = dv.list_developments(live_only=True)
        template = dv.get_template()
    except Exception as e:
        logger.warning("What's New: could not load: %s", e)
        error = str(getattr(e, "detail", None) or e)
    for dev in developments:
        page = pages.get(dev["page_key"])
        dev["page_name"] = page["name"] if page else ""
        dev["page_url"] = BASE_URL + page["path"] if page else ""
        dev["can_open"] = bool(page) and user_can(user, dev["page_key"])
    return templates.TemplateResponse("whats_new.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "developments": developments, "template": template, "error": error,
    })
