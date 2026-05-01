from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
import traceback
import logging
import os

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

BASE_URL = os.getenv("BASE_URL", "")


_STATUS_TITLES = {
    400: "Bad Request",
    403: "Access Denied",
    404: "Not Found",
    409: "Conflict",
    500: "Server Error",
    503: "Service Unavailable",
}

_DB_KEYWORDS = ("connection timed out", "connection refused", "could not connect", "vpn", "check vpn", "server closed the connection")


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
                "details": raw.strip() if raw.strip() else None,
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
                "details": str(exc),
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
