from fastapi import Request
from fastapi.responses import JSONResponse
import traceback
import logging
import os

logger = logging.getLogger(__name__)


async def unhandled_exception_handler(request: Request, exc: Exception):
    tb = "".join(traceback.format_exception(exc))
    content = {
        "message": "An internal server error occurred.",
        "details": str(exc),
        "traceback": tb
    }

    # Return a JSON response with the error message and traceback
    return JSONResponse(
        status_code=500,
        content=content,
    )
