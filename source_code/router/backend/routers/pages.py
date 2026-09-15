"""Home page (with background cache warming) and stock list page."""
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
import logging



from .core import (BASE_URL, templates, _track, get_current_user, require_page,
                   get_user_pages, is_admin)
from .page_registry import PAGES

logger = logging.getLogger(__name__)

router = APIRouter()
from . import kpi
from . import orders_deliveries
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
        def _od():
            try:
                orders_deliveries.warm_cache()
            except Exception as e:
                logger.warning(f"Home cache warm (Orders & Deliveries) failed: {e}")
        def _apis():
            # Build the CAP and Autotrader clients and get an AT access token
            # now. Both read credentials from AWS Secrets Manager in their
            # constructors, and both are singletons, so doing it here means a
            # manual vehicle check does not pay for the secret fetch or the auth
            # round trip on its first use.
            try:
                import requests as _requests
                from list_processor.cap_client import CAPClient
                from list_processor.autotrader_client import AutoTraderClient
                CAPClient()
                AutoTraderClient().get_access_token(_requests.Session())
            except Exception as e:
                logger.warning(f"Home cache warm (CAP/Autotrader credentials) failed: {e}")
        with ThreadPoolExecutor(max_workers=6) as ex:
            ex.submit(_sf); ex.submit(_dbs); ex.submit(_cb); ex.submit(_sp); ex.submit(_od)
            ex.submit(_apis)
    threading.Thread(target=_warm_caches, daemon=True).start()
    _track(request, 'page_view', '/home')
    # Only the pages this user may open. /home itself is deliberately not in the
    # registry — everyone who is logged in needs to land somewhere, including a
    # user granted nothing at all.
    allowed = get_user_pages(user)
    return templates.TemplateResponse("home.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "pages": [p for p in PAGES if p.key in allowed],
        "is_admin": is_admin(user),
    })


# NEW
@router.get("/stock-list", response_class=HTMLResponse)
def stock_list(request: Request, user: str = Depends(require_page('stock_list'))):
    _track(request, 'page_view', '/stock-list')
    return templates.TemplateResponse("stock_list.html", {"request": request, "user": user, "BASE_URL": BASE_URL})
