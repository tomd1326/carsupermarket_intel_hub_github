"""The Technical Guide (/technical-guide): how the whole site is built, for a new
developer. Admin only. The text lives in templates/technical_guide_content.html,
next to the code it describes, so it is changed in the same merge request as the
code. The contents list down the left is built in the page from its headings.
The text size and left menu width are the User Guide's (Settings > User Guide)."""

import logging

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse

from utils import user_guide as ug
from .core import BASE_URL, templates, get_current_user, is_admin, _track, PageRedirect

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/technical-guide", response_class=HTMLResponse)
def technical_guide_page(request: Request, user: str = Depends(get_current_user)):
    if not user:
        raise PageRedirect(BASE_URL + "/login")
    if not is_admin(user):
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    _track(request, 'page_view', '/technical-guide')
    settings = dict(ug.SETTINGS_DEFAULTS)
    try:
        _, settings = ug.read_guide()
    except Exception as e:
        logger.warning("Technical Guide: could not read the User Guide settings: %s", e)
    return templates.TemplateResponse("technical_guide.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL, "settings": settings,
    })
