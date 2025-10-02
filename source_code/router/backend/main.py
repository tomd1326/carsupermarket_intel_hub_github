# FILE PATH: source_code/router/backend/main.py

import logging
import os
import re
import json
import signal
import sys
import pathlib
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from mangum import Mangum

from router.backend.exceptions import unhandled_exception_handler
from router.backend.utils.secrets_manager_client import get_secret
from router.backend.utils.lambda_http import LambdaRequestsManager
import router.backend.routes.task_routes as task_routes

# ---------- logging ----------
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger().handlers.clear()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger("app")

for name, lvl in {
    "botocore": logging.CRITICAL,
    "boto3": logging.CRITICAL,
    "s3transfer": logging.CRITICAL,
    "urllib3": logging.CRITICAL,
    "asyncio": logging.CRITICAL,
    "multipart": logging.CRITICAL,
    "python_multipart": logging.WARNING,
    "utils.secrets_manager_client": logging.WARNING,
    "list_processor.autotrader_client": logging.WARNING,
    "list_processor.cap_client": logging.WARNING,
    "utils.s3_tools": logging.WARNING,
}.items():
    logging.getLogger(name).setLevel(lvl)

# Import router
try:
    from router.backend.api_router import router as api_router
    logger.info("Router loaded successfully")
except Exception as e:
    logger.error("Router import failed: %s", e)
    raise

# ---------- runtime config ----------
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
API_BASE_PATH = os.getenv("API_GATEWAY_BASE_PATH", "/dev")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-in-prod")

# ---------- middleware ----------
class BasePathMiddleware(BaseHTTPMiddleware):
    """Strip API Gateway base path (stage) so FastAPI sees clean routes."""
    def __init__(self, app: FastAPI, base_path: Optional[str]) -> None:
        super().__init__(app)
        self._pattern = re.compile(rf"^/{re.escape(base_path.lstrip('/'))}") if base_path else None
    async def dispatch(self, request: Request, call_next):
        if self._pattern:
            new_path = self._pattern.sub("", request.url.path) or "/"
            request.scope["path"] = new_path
        return await call_next(request)

# ---------- lifespan ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application starting up")
    url = f"http://{APP_HOST}:{APP_PORT}"
    logger.info("Server ready: %s", url)
    print(f"Server ready: {url}", flush=True)
    yield

    logger.info("Application shutting down: running cleanup")

    try:
        with task_routes.task_events_lock:
            for ev in list(task_routes.task_events.values()):
                try:
                    ev.set()
                except Exception:
                    pass
            task_routes.task_events.clear()
    except Exception:
        logger.exception("Error notifying task events")

    logger.info("Application shutdown complete")

# ---------- app ----------
app = FastAPI(
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

# Sessions - MUST be added before routers
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# Base path strip for API Gateway stage, e.g. "/dev"
if API_BASE_PATH and API_BASE_PATH != "/":
    app.add_middleware(BasePathMiddleware, base_path=API_BASE_PATH)

# ---- Mount static directory (Lambda-safe) ----
STATIC_DIR = pathlib.Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Include routers AFTER middleware
app.include_router(api_router)

# Exceptions
app.add_exception_handler(Exception, unhandled_exception_handler)

# ---------- utility endpoints ----------
@app.get("/healthcheck")
def simple_healthcheck(request: Request):
    ctx = request.scope.get("aws.context")
    return {"status": "ok", "context": json.dumps(ctx, default=str) if ctx else None}

@app.get("/test")
def read_root(request: Request):
    res = {"Hello": "World2"}
    ctx = request.scope.get("aws.context")
    lambda_requests_manager.simulate_http(
        aws_context=ctx,
        path="/test_execution",
        method="POST",
        data=res,
    )
    return res

@app.get("/test-secrets-manager")
def test_secrets_manager(request: Request):
    env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"
    varname = f"{env}/common/car_pricer/rds/admin_credentials"
    data = get_secret(varname)
    return {"status": "ok", "data": data}

@app.get("/test-db")
def test_db(request: Request):
    """Test connection to Aurora PostgreSQL. Returns rows from test_table."""
    import psycopg2
    import psycopg2.extras

    env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"
    varname = f"{env}/common/car_pricer/rds/admin_credentials"
    creds = get_secret(varname)

    conn = psycopg2.connect(
        host=creds["cluster_endpoint"],
        user=creds["username"],
        password=creds["password"],
        database="postgres",
        port=5432,
    )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM test_table")
            rows = cur.fetchall()
        result = [dict(r) for r in rows]
    finally:
        conn.close()

    return {"status": "ok", "data": result}

@app.post("/test_execution")
def test_execution(data: dict):
    logger.info("test_execution invoked")
    logger.info(data)
    return {}

# ---------- lambda adapter ----------
handler = Mangum(app, lifespan="auto")
lambda_requests_manager = LambdaRequestsManager()
lambda_requests_manager.init(mangum_handler=handler)

# ---------- signal handler for clean Ctrl+C ----------
def handle_sigterm(*args):
    logger.info("Received SIGTERM, shutting down gracefully")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)
