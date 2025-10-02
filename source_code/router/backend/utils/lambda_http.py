# FILE PATH: source_code/router/backend/utils/lambda_http.py

from typing import Optional
import threading
import httpx

from .config import PER_HOST_LIMIT, HTTP_TIMEOUT_S

# One httpx.AsyncClient per thread/event-loop
# Avoids "Event loop is closed" when background threads create their own loops.
_thread_local = threading.local()


def get_http_client() -> httpx.AsyncClient:
    """
    Return a thread-local httpx.AsyncClient configured for high throughput.

    - HTTP/2 enabled
    - Large connection pool per host (PER_HOST_LIMIT)
    - Keep-Alive headers
    - Reused within the same background thread/event loop
    """
    client: Optional[httpx.AsyncClient] = getattr(_thread_local, "client", None)
    if client is not None:
        return client

    limits = httpx.Limits(
        max_connections=PER_HOST_LIMIT,
        max_keepalive_connections=PER_HOST_LIMIT,
    )
    timeout = httpx.Timeout(HTTP_TIMEOUT_S)

    client = httpx.AsyncClient(
        http2=True,
        limits=limits,
        timeout=timeout,
        headers={
            "Connection": "keep-alive",
            "User-Agent": "CarSupermarket/1.0 (+lambda-http)",
        },
    )
    _thread_local.client = client
    return client


async def close_http_client():
    """Close the current thread's client (optional graceful shutdown)."""
    client: Optional[httpx.AsyncClient] = getattr(_thread_local, "client", None)
    if client is not None:
        try:
            await client.aclose()
        finally:
            _thread_local.client = None


# Backward-compat shim used elsewhere in the app
class LambdaRequestsManager:
    """
    Legacy shim so existing code can import/init without errors.
    New code should use get_http_client() directly.
    """
    def __init__(self) -> None:
        self._state = {}

    def init(self, **kwargs):
        self._state.update(kwargs)
        return self