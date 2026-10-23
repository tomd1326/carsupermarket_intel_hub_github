"""Email Attachment Extractor: save every attachment from an email, including the
attachments of every email attached inside it, into a folder the user picks.

The .msg file is read in the browser (templates/email_attachments.html), not here.
That keeps it under no upload size limit, and the email never leaves the user's PC.
This router only serves the page and the Settings > Email Attachments values.
"""
import logging

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse

from utils.db_utils import get_database_connection, get_settings

from .core import BASE_URL, templates, _track, require_page

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/email-attachments", response_class=HTMLResponse)
def email_attachments_page(request: Request, user: str = Depends(require_page('email_attachments'))):
    _track(request, 'page_view', '/email-attachments')
    with get_database_connection() as conn:
        settings = get_settings(conn)
    return templates.TemplateResponse("email_attachments.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "skip_inline": bool(settings.get('email_attach_skip_inline', 1)),
        "inline_keep_kb": int(settings.get('email_attach_inline_keep_kb', 100)),
    })
