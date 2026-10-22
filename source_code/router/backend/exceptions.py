from fastapi import Request, HTTPException
from fastapi.templating import Jinja2Templates
import traceback
import logging
import os

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

BASE_URL = os.getenv("BASE_URL", "")

# error.html uses static_url, the same as every other template. This is a second
# Jinja2Templates instance to the one in routers/core.py, and globals are per
# instance — without this registration the error page itself raises
# "'static_url' is undefined" while rendering, so the real error is replaced by
# a Jinja traceback. Imported lazily to avoid a circular import: core imports
# page_registry, and main imports both.
#
# The page header (page_header in _navigation.html) also needs the other
# helpers core registers (presence_config, user_details, user_menu, ...), so
# all of core's globals are copied across. Each of those keeps its last known
# values if the database is down, so the error page still draws.
def _register_globals():
    try:
        from routers import core
        templates.env.globals.update(core.templates.env.globals)
    except Exception:  # pragma: no cover - fall back to an unversioned path
        templates.env.globals['static_url'] = lambda path: f"{BASE_URL}/static/{path.lstrip('/')}"


_register_globals()


_STATUS_TITLES = {
    400: "Bad Request",
    403: "Access Denied",
    404: "Not Found",
    409: "Conflict",
    500: "Server Error",
    503: "Service Unavailable",
}

_DB_KEYWORDS = ("connection timed out", "connection refused", "could not connect", "vpn", "check vpn", "server closed the connection")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return text.strip()


def _is_db_connection_error(msg: str) -> bool:
    lower = msg.lower()
    return any(kw in lower for kw in _DB_KEYWORDS)


async def http_exception_handler(request: Request, exc: HTTPException):
    detail = str(exc.detail)
    is_db = _is_db_connection_error(detail)
    title = _STATUS_TITLES.get(exc.status_code, f"Error {exc.status_code}")

    if is_db:
        # Split "Database connection failed — check VPN/network connectivity. Detail: <raw>"
        # into a clean message + collapsed raw detail
        clean, _, raw = detail.partition(". Detail:")
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "BASE_URL": BASE_URL,
                "error_type": "db_connection",
                "title": "Database Unreachable",
                "message": clean.strip(),
                "hint": "Connect to the VPN and try again.",
                "details": _first_line(raw) if raw.strip() else None,
            },
            status_code=exc.status_code,
        )

    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "BASE_URL": BASE_URL,
            "error_type": "http",
            "title": title,
            "message": detail,
            "hint": None,
            "details": None,
        },
        status_code=exc.status_code,
    )


async def unhandled_exception_handler(request: Request, exc: Exception):
    tb = "".join(traceback.format_exception(exc))
    logger.error("Unhandled exception on %s %s: %s: %s", request.method, request.url.path, type(exc).__name__, exc)
    logger.debug("Traceback:\n%s", tb)

    if _is_db_connection_error(str(exc)):
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "BASE_URL": BASE_URL,
                "error_type": "db_connection",
                "title": "Database Unreachable",
                "message": "Could not connect to the database. This usually means you are not connected to the VPN.",
                "hint": "Connect to the VPN and try again.",
                "details": _first_line(str(exc)),
            },
            status_code=500,
        )

    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "BASE_URL": BASE_URL,
            "error_type": "internal",
            "title": "Internal Server Error",
            "message": "An unexpected error occurred.",
            "hint": None,
            "details": tb,
        },
        status_code=500,
    )
