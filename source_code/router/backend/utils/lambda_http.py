# FILE: source_code/router/backend/utils/lambda_http.py

import asyncio
from typing import Optional
import httpx

from .config import PER_HOST_LIMIT, HTTP_TIMEOUT_S

# Module-level singleton AsyncClient reused across Lambda invocations.
_client: Optional[httpx.AsyncClient] = None
_init_lock = asyncio.Lock()

async def get_http_client() -> httpx.AsyncClient:
    """
    Returns a shared httpx.AsyncClient with HTTP/2, connection pooling,
    and keep-alive tuned for high throughput on AWS Lambda.
    """
    global _client
    if _client is not None:
        return _client

    async with _init_lock:
        if _client is None:
            limits = httpx.Limits(
                max_keepalive_connections=PER_HOST_LIMIT,
                max_connections=PER_HOST_LIMIT,
            )
            timeout = httpx.Timeout(HTTP_TIMEOUT_S)
            _client = httpx.AsyncClient(
                limits=limits,
                timeout=timeout,
                http2=True,
                headers={
                    "Connection": "keep-alive",
                    "User-Agent": "CarSupermarket-Lambda/1.0",
                },
            )
    return _client

async def close_http_client():
    """
    Optional helper for local dev or graceful shutdowns.
    Lambda typically does not require explicit close.
    """
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None

# ---------- Backward-compat shim ----------
class LambdaRequestsManager:  # deprecated
    """
    Legacy shim to satisfy existing imports and calls.
    New code should use: `await get_http_client()`.
    """
    def __init__(self) -> None:
        self._state = {}

    def init(self, **kwargs):
        # Accepts e.g. mangum_handler=handler. No-op for local run.
        self._state.update(kwargs)
        return self