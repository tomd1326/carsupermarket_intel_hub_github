# FILE PATH: source_code/router/backend/main.py

import logging
import os
import re
import json
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from mangum import Mangum

from exceptions import unhandled_exception_handler
from utils.secrets_manager_client import get_secret
from utils.lambda_http import LambdaRequestsManager

# ---------- logging ----------
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

# Import router - FIXED: Changed from router2 to router
try:
    from router import router as api_router
    logger.info("Router loaded successfully")
except Exception as e:
    logger.error("Router import failed: %s", e)
    raise

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
    yield
    logger.info("Application shutting down")

# ---------- app ----------
app = FastAPI(lifespan=lifespan)

# Sessions - MUST be added before routers
session_secret = os.getenv("SESSION_SECRET", "change-me-in-prod")
app.add_middleware(SessionMiddleware, secret_key=session_secret)

# Base path strip for API Gateway stage, e.g. "/dev"
api_base_path = os.getenv("API_GATEWAY_BASE_PATH", "/dev")
if api_base_path and api_base_path != "/":
    app.add_middleware(BasePathMiddleware, base_path=api_base_path)

# Include routers AFTER session middleware
app.include_router(api_router)

# Exceptions
app.add_exception_handler(Exception, unhandled_exception_handler)

# REMOVED: Noisy route registration logging

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

# ---------- local dev ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True, workers=1)