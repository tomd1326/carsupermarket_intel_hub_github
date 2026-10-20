"""Domain routers, combined into a single APIRouter for main.py.

Module order matters only for FastAPI route registration; all paths are
distinct so the order is not behaviour-relevant. core is imported first so
shared state (user reload from Aurora) initialises before any routes.
"""
from fastapi import APIRouter

from . import core  # noqa: F401 — initialises shared state on import
from . import auth, settings, pages, kpi, buying_requirements, cap_lookup, mot_check, tasks, preferences, reports, orders_deliveries, subsidy_rates, assess_vehicle, keeper_change, presence, sharepoint, api_requests, dbt_runs, logs

router = APIRouter(
    prefix="",
    tags=["Main Router"],
    responses={404: {"description": "Not found"}},
)

for _module in (auth, settings, pages, kpi, buying_requirements, cap_lookup, mot_check, tasks, preferences, reports, orders_deliveries, subsidy_rates, assess_vehicle, keeper_change, presence, sharepoint, api_requests, dbt_runs, logs):
    router.include_router(_module.router)
