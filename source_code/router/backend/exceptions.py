from fastapi import Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
import traceback
import logging
import os

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

BASE_URL = os.getenv("BASE_URL", "")


def _is_db_connection_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "connection timed out" in msg or "connection refused" in msg or "could not connect" in msg


async def unhandled_exception_handler(request: Request, exc: Exception):
    tb = "".join(traceback.format_exception(exc))
    logger.error("Unhandled exception: %s\n%s", exc, tb)

    if _is_db_connection_error(exc):
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
