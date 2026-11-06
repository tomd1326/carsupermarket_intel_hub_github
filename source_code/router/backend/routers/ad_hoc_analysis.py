"""Ad-Hoc Analysis page: one-off pieces of analysis, each its own section.

Every analysis has its own page under /ad-hoc-analysis, with all of them listed
in the bar down the left; /ad-hoc-analysis itself opens the first one. One page
key covers them all, so granting Ad-Hoc Analysis on Settings > User Accounts
opens every analysis listed here.

Adding an analysis: add it to ANALYSES, give it a route below, a template and
(if it has one) a JS file. Pass `analyses` to the template so the left bar lists them."""
import threading
import time

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
import logging

from utils.snowflake_client import SnowflakeClient

from .core import BASE_URL, templates, _track, require_page, require_page_api, is_admin
from . import kpi
from .buying_requirements import _sf_cache_is_fresh

logger = logging.getLogger(__name__)

router = APIRouter()

PAGE_KEY = 'ad_hoc_analysis'
_PATH = '/ad-hoc-analysis'

# Shown on /ad-hoc-analysis, newest first.
ANALYSES = [
    {"slug": "buyer-estimates",
     "title": "Deliveries: Buyer Estimates vs Original vs Actual",
     "period": "May to Oct 2026",
     "description": "Buyer RRP, prep, metal and GPU-M from the Cars Bought sheet against the first advertised "
                    "price and what each delivery actually made, by month and week, with the cars behind every bar."},
]


@router.get(_PATH)
def ad_hoc_analysis(request: Request, user: str = Depends(require_page(PAGE_KEY))):
    """Opens the first analysis; the others are in its left bar."""
    return RedirectResponse(f"{BASE_URL}{_PATH}/{ANALYSES[0]['slug']}", status_code=302)


# ── Deliveries: Buyer Estimates vs Original vs Actual ──
# A fixed period, as the written Summary on the page describes these months. One row per
# delivery; the page works out every chart from these rows, so the charts, the filters
# and the cars behind each bar always agree.

_BE_PATH = _PATH + '/buyer-estimates'
# Invoice posted from 1 May 2026 up to the end of ISO week 44 (Sunday 1 Nov 2026).
_BE_FROM, _BE_TO = '2026-05-01', '2026-11-02'
# Standard admin fee inc VAT, added to the three prices: the table holds them without it.
_BE_ADMIN_FEE = 199
_BE_SQL = f"""
SELECT
    vrm AS VRM,
    supplier_name AS SUPPLIER,
    CASE WHEN supplier_name = 'Z001' THEN 'BCB' WHEN supplier_channel = 'B2B2C' THEN 'B2B2C'
         WHEN supplier_channel = 'C2B2C - PX' THEN 'PX' WHEN supplier_channel = 'C2B2C - Other' THEN 'Other' END AS CHANNEL,
    mileage AS MILEAGE,
    date_first_registered AS REGISTERED,
    purchase_price_inc_vat AS PURCHASE_INC_VAT,
    NULLIF(IFF(sales_type_bucket = '0%', buyer_rrp_zero_pct, buyer_rrp), 0) + {_BE_ADMIN_FEE} AS BUYER_RRP,
    buyer_est_chassis_margin_after_subsidy AS BUYER_CHASSIS,
    buyer_est_prep AS BUYER_RECON,
    buyer_est_metal_margin AS BUYER_METAL,
    gpu_m_costs AS GPU_M_COSTS,
    buyer_est_gpu_m AS BUYER_GPU_M,
    NULLIF(original_price_inc_vat, 0) + {_BE_ADMIN_FEE} AS ORIGINAL_PRICE,
    original_chassis_margin_after_subsidy AS ORIGINAL_CHASSIS,
    original_metal_margin AS ORIGINAL_METAL,
    original_gpu_m AS ORIGINAL_GPU_M,
    NULLIF(sale_price_inc_vat, 0) + {_BE_ADMIN_FEE} AS SELLING_PRICE,
    discount_inc_vat AS DISCOUNT,
    chassis_margin_after_subsidy AS ACTUAL_CHASSIS,
    COALESCE(recon_costs_autoview, vehicle_costs) AS ACTUAL_RECON,
    recon_costs_autoview AS AUTOVIEW_RECON,
    other_costs_autoview AS AUTOVIEW_OTHER_COSTS,
    vehicle_costs AS APEX_VEHICLE_COSTS,
    write_down AS WRITE_DOWN,
    finance_subsidy AS FINANCE_SUBSIDY,
    metal_margin AS ACTUAL_METAL,
    gpu_m AS ACTUAL_GPU_M,
    acknowledged_days_in_stock AS DAYS_IN_STOCK,
    acknowledged_days_advertised AS DAYS_TO_SELL,
    sales_type_bucket AS SALES_TYPE,
    invoice_posted_date AS INVOICE_POSTED,
    make_model AS MAKE_MODEL,
    stock_id AS STOCK_ID
FROM {kpi._VEHICLE_DB}.PRODUCT_SALES_PERFORMANCE.SALES_PERFORMANCE
WHERE invoice_posted_at >= '{_BE_FROM}'
    AND invoice_posted_at < '{_BE_TO}'
    AND invoice_credited = FALSE
    AND supplier_channel IN ('B2B2C', 'C2B2C - PX', 'C2B2C - Other')
ORDER BY invoice_posted_date, vrm
"""

_be_cache: dict = {"columns": None, "rows": None, "cached_at": 0.0}
_be_cache_lock = threading.Lock()


@router.get(_BE_PATH, response_class=HTMLResponse)
def buyer_estimates(request: Request, user: str = Depends(require_page(PAGE_KEY))):
    _track(request, 'page_view', _BE_PATH)
    analysis = next(a for a in ANALYSES if a["slug"] == "buyer-estimates")
    return templates.TemplateResponse("ad_hoc_buyer_estimates.html", {"request": request, "user": user, "BASE_URL": BASE_URL,
                                                                      "analysis": analysis, "analyses": ANALYSES,
                                                                      "is_admin": is_admin(user)})


@router.get(_BE_PATH + "/data")
def buyer_estimates_data(request: Request, force: bool = False, user: str = Depends(require_page_api(PAGE_KEY))):
    """One row per delivery in the period, kept until dbt next rebuilds the tables."""
    try:
        with _be_cache_lock:
            cache = dict(_be_cache)
        if force or cache["rows"] is None or not _sf_cache_is_fresh(cache["cached_at"]):
            with SnowflakeClient() as sf:
                # No parameters: the SQL holds a '0%' literal, and the dates are fixed above.
                rows, columns = sf.execute(_BE_SQL)
            cache = {"columns": [c.upper() for c in columns], "cached_at": time.time(),
                     "rows": [[kpi._json_cell(v) for v in r] for r in rows]}
            with _be_cache_lock:
                _be_cache.update(cache)
    except Exception as e:
        logger.error(f"Ad-hoc buyer estimates data failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "columns": cache["columns"], "rows": cache["rows"], "cached_at": cache["cached_at"]}
