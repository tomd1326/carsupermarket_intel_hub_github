"""Per-user UI preferences (e.g. chosen table columns), stored in Aurora."""
import json
import re
import logging

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse

from utils.db_utils import (get_database_connection, get_user_preference,
                            save_user_preference, delete_user_preference)

from .core import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

_PREF_KEY_RE = re.compile(r'^[a-z0-9_]{1,100}$')
_MAX_VALUE_BYTES = 16 * 1024


def _bad_key(pref_key: str) -> JSONResponse | None:
    if not _PREF_KEY_RE.match(pref_key):
        return JSONResponse({"status": "error", "message": "invalid preference key"}, status_code=400)
    return None


@router.get("/api/user-prefs/{pref_key}")
def read_user_pref(pref_key: str, user: str = Depends(get_current_user)):
    if not user:
        return JSONResponse({"status": "error", "message": "not authenticated"}, status_code=401)
    if (err := _bad_key(pref_key)):
        return err
    try:
        with get_database_connection() as conn:
            value = get_user_preference(conn, user, pref_key)
        return {"status": "ok", "value": value}
    except Exception as e:
        logger.error(f"Read user pref {pref_key} failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.put("/api/user-prefs/{pref_key}")
async def write_user_pref(pref_key: str, request: Request, user: str = Depends(get_current_user)):
    if not user:
        return JSONResponse({"status": "error", "message": "not authenticated"}, status_code=401)
    if (err := _bad_key(pref_key)):
        return err
    body = await request.body()
    if len(body) > _MAX_VALUE_BYTES:
        return JSONResponse({"status": "error", "message": "preference value too large"}, status_code=413)
    try:
        value = json.loads(body)
    except Exception:
        return JSONResponse({"status": "error", "message": "body must be valid JSON"}, status_code=400)
    try:
        with get_database_connection() as conn:
            save_user_preference(conn, user, pref_key, value)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Save user pref {pref_key} failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@router.delete("/api/user-prefs/{pref_key}")
def remove_user_pref(pref_key: str, user: str = Depends(get_current_user)):
    if not user:
        return JSONResponse({"status": "error", "message": "not authenticated"}, status_code=401)
    if (err := _bad_key(pref_key)):
        return err
    try:
        with get_database_connection() as conn:
            delete_user_preference(conn, user, pref_key)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Delete user pref {pref_key} failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
