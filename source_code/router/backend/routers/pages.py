"""Home page (with background cache warming) and stock list page."""
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
import logging



from .core import (BASE_URL, templates, _track, get_current_user)

logger = logging.getLogger(__name__)

router = APIRouter()
from . import kpi
from .buying_requirements import _get_sf_rows, _get_dbs_purchases, _get_cb_purchases


# NEW
@router.get("/home", response_class=HTMLResponse)
def home(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    def _warm_caches():
        def _sf():
            try:
                _get_sf_rows()
            except Exception as e:
                logger.warning(f"Home cache warm (Snowflake) failed: {e}")
        def _dbs():
            try:
                _get_dbs_purchases()
            except Exception as e:
                logger.warning(f"Home cache warm (DBS) failed: {e}")
        def _cb():
            try:
                _get_cb_purchases()
            except Exception as e:
                logger.warning(f"Home cache warm (CB) failed: {e}")
        def _sp():
            try:
                kpi.warm_sharepoint_cache()
            except Exception as e:
                logger.warning(f"Home cache warm (SharePoint KPI) failed: {e}")
        with ThreadPoolExecutor(max_workers=4) as ex:
            ex.submit(_sf); ex.submit(_dbs); ex.submit(_cb); ex.submit(_sp)
    threading.Thread(target=_warm_caches, daemon=True).start()
    _track(request, 'page_view', '/home')
    return templates.TemplateResponse("home.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


# NEW
@router.get("/stock-list", response_class=HTMLResponse)
def stock_list(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    _track(request, 'page_view', '/stock-list')
    return templates.TemplateResponse("stock_list.html", {"request": request, "user": user, "BASE_URL": BASE_URL})
