"""Self-Analytics page (/self-analytics): run a Snowflake query and chart the
rows. Anyone with the page opens saved reports shared with them (their SQL is
run from the saved copy, never from the browser). Writing and running your own
SQL needs the admin or the Run Own Queries permission. JSON APIs behind
static/js/self_analytics.js; the work is in utils/self_analytics.py."""

import json
import logging

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse

from utils import self_analytics as sa
from utils import ai_knowledge as ak
from utils import self_analytics_builder as builder
from fastapi.responses import Response

from .core import (BASE_URL, VALID_USERS, templates, is_admin, user_details, user_has_permission, require_page,
                   require_page_api, _track, _uk_now)

logger = logging.getLogger(__name__)

router = APIRouter()

PAGE_KEY = "self_analytics"
SQL_PERMISSION = "self_analytics_sql"


def _can_write_sql(user: str) -> bool:
    return user_has_permission(user, SQL_PERMISSION)


def _people() -> list:
    """Everyone who can sign in, as {'username', 'name'}, for the sharing list."""
    people = []
    for username in VALID_USERS:
        details = user_details(username)
        name = f"{details['first_name']} {details['last_name']}".strip()
        people.append({"username": username, "name": name or username})
    return sorted(people, key=lambda p: p["name"].lower())


def _for_viewer(report: dict) -> dict:
    """What someone without the SQL permission gets: no SQL, no sharing list."""
    return {**report, "sql": "", "users": []}


def _semantic_hints(user: str) -> list:
    """Approved Semantic & Context Layer entries, cut down to what the page
    needs: words that name a column (Also Called, title) and its definition."""
    try:
        settings = ak.get_reference_settings()
        entries = ak.list_entries()
    except Exception as e:
        logger.warning("Self-Analytics: semantic layer not read: %s", e)
        return []
    hints = []
    for e in entries:
        if e["status"] != "active" or not e.get("enabled", True) or not e.get("show_on_page", True):
            continue
        if not is_admin(user) and e["kind"] not in settings["kinds_shown"]:
            continue
        f = e.get("fields") or {}
        columns = f.get("source_columns", "")
        if e["kind"] == "column":
            columns = e["title"].rsplit(".", 1)[-1]
        hints.append({"title": e["title"], "kind": e["kind"], "columns": columns,
                      "synonyms": f.get("synonyms", ""), "definition": f.get("definition", "") or e.get("content", "")[:400]})
    return hints


@router.get("/self-analytics", response_class=HTMLResponse)
def self_analytics_page(request: Request, user: str = Depends(require_page(PAGE_KEY))):
    _track(request, 'page_view', '/self-analytics')
    return templates.TemplateResponse("self_analytics.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "can_write_sql": _can_write_sql(user), "show_admin": is_admin(user),
    })


@router.get("/self-analytics/data")
def self_analytics_data(request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    admin = is_admin(user)
    can_sql = _can_write_sql(user)
    reports = sa.list_reports(user, admin)
    if not can_sql:
        # People who cannot run their own SQL never see it.
        reports = [_for_viewer(r) for r in reports]
    return {
        "settings": sa.get_settings(), "reports": reports, "hints": _semantic_hints(user),
        "can_write_sql": can_sql, "is_admin": admin, "user": user,
        "people": _people() if can_sql else [],
        "options": {"chart_types": sa.CHART_TYPES, "grains": sa.GRAINS, "aggs": sa.AGGS,
                    "filter_ops": sa.FILTER_OPS, "formats": sa.FORMATS, "sorts": sa.SORTS,
                    "visibilities": sa.VISIBILITIES},
    }


@router.post("/self-analytics/run")
def self_analytics_run(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    """Run typed SQL (needs the permission), or a saved report by id. A saved
    report's rows are reused for the cache time unless refresh is sent."""
    payload = payload or {}
    now_text = _uk_now().strftime("%d %b %Y %H:%M")
    if not payload.get("report_id") and not _can_write_sql(user):
        raise HTTPException(status_code=403, detail="You can open saved reports but not run your own SQL.")
    try:
        settings = sa.get_settings()
        if payload.get("report_id"):
            try:
                report = sa.get_report(int(payload["report_id"]))
            except (TypeError, ValueError):
                report = None
            if not report or not sa.can_see(report, user, is_admin(user)):
                return {"status": "error", "message": "That report is not available."}
            result = sa.run_report(report, settings, bool(payload.get("refresh")), now_text)
        else:
            result = {**sa.run_query(payload.get("sql"), settings), "as_at": now_text, "from_cache": False}
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.warning("Self-Analytics query by %s failed: %s", user, e)
        return {"status": "error", "message": f"Snowflake said: {str(e)[:800]}"}
    _track(request, 'self_analytics_run', '/self-analytics', {"rows": result["row_count"]})
    return {"status": "ok", **result}


def _builder_access(user):
    if not _can_write_sql(user):
        raise HTTPException(status_code=403, detail="Report Building Requires Run Own Queries Permission")
    if not sa.get_settings().get("builder_enabled"):
        raise HTTPException(status_code=403, detail="Enable Guided Reports In Self-Analytics Settings First")


@router.get("/self-analytics/builder/catalog")
def builder_catalog(user: str = Depends(require_page_api(PAGE_KEY))):
    _builder_access(user)
    return builder.catalog()


@router.post("/self-analytics/builder/source")
def builder_source(payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    _builder_access(user)
    try:
        return {"source": builder.source(payload.get("id"))}
    except (ValueError, KeyError) as exc:
        return {"status": "error", "message": str(exc)}


@router.post("/self-analytics/builder/preview")
def builder_preview(payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    _builder_access(user)
    try:
        return builder.prepare(payload)
    except (ValueError, TypeError, KeyError) as exc:
        return {"status": "error", "message": str(exc)}


@router.post("/self-analytics/builder/run")
def builder_run(payload: dict, request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    _builder_access(user)
    try:
        plan = builder.prepare(payload)
        if payload.get("confirmed") is not True or payload.get("review_hash") != plan["review_hash"]:
            raise ValueError("Review And Confirm The Current Report Before Running")
        result = builder.run(plan["source_spec"], plan["charts"], sa.get_settings())
        _track(request, 'self_analytics_run', '/self-analytics', {"rows": result["row_count"]})
        return {"status": "ok", "plan": plan, **result, "as_at": _uk_now().strftime("%d %b %Y %H:%M"), "from_cache": False}
    except (ValueError, TypeError, KeyError) as exc:
        return {"status": "error", "message": str(exc)}
    except Exception:
        logger.exception("Guided report failed for %s", user)
        return {"status": "error", "message": "The Source Could Not Be Read. Check Its Configuration And Availability; No Chart Was Built."}


@router.post("/self-analytics/save")
def self_analytics_save(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    if not _can_write_sql(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        report = sa.save_report(payload or {}, user, is_admin(user), set(VALID_USERS))
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    logger.info("Self-Analytics report '%s' saved by %s", report["name"], user)
    return {"status": "ok", "report": report}


@router.post("/self-analytics/import")
def self_analytics_import(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    """One report file, or {"reports": [...]} (docs/self_analytics_report_format.md)."""
    if not _can_write_sql(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        saved = sa.import_reports(payload or {}, user, is_admin(user), set(VALID_USERS))
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    logger.info("Self-Analytics: %s reports imported by %s", len(saved), user)
    return {"status": "ok", "reports": saved}


@router.get("/self-analytics/export/{report_id}")
def self_analytics_export(report_id: int, request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    if not _can_write_sql(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    report = sa.get_report(report_id)
    if not report or not sa.can_see(report, user, is_admin(user)):
        raise HTTPException(status_code=404, detail="That report is not available.")
    filename = "".join(c if c.isalnum() or c in "-_ " else "_" for c in report["name"]).strip() or "report"
    return Response(content=json.dumps(sa.export_report(report), indent=2, ensure_ascii=False),
                    media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{filename}.json"'})


@router.post("/self-analytics/delete")
def self_analytics_delete(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    if not _can_write_sql(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        sa.delete_report(int((payload or {}).get("id")), user, is_admin(user))
    except (TypeError, ValueError) as e:
        return {"status": "error", "message": str(e) or "No report given."}
    logger.info("Self-Analytics report %s deleted by %s", (payload or {}).get("id"), user)
    return {"status": "ok"}


@router.post("/self-analytics/settings")
def self_analytics_settings(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    settings = sa.save_settings(payload or {}, user)
    logger.info("Self-Analytics settings saved by %s", user)
    return {"status": "ok", "settings": settings}


@router.post("/self-analytics/settings/reset")
def self_analytics_settings_reset(request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"status": "ok", "settings": sa.reset_settings()}
