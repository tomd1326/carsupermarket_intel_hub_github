import logging
import os
import re
import time

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.status import HTTP_401_UNAUTHORIZED
from mangum import Mangum
import json
import hashlib

from exceptions import unhandled_exception_handler, http_exception_handler
from utils.secrets_manager_client import get_secret
from utils.lambda_http import LambdaRequestsManager
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
import routers
from routers.core import PageRedirect


app = FastAPI()


logger = logging.getLogger(__name__)

# Force configure logging for AWS Lambda
logging.getLogger().handlers.clear()
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", force=True)

# Disable useless logging
logging.getLogger('botocore').setLevel(logging.CRITICAL)
logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('s3transfer').setLevel(logging.CRITICAL)
logging.getLogger('urllib3').setLevel(logging.CRITICAL)
logging.getLogger('asyncio').setLevel(logging.CRITICAL)
logging.getLogger('multipart').setLevel(logging.CRITICAL)
logging.getLogger('snowflake').setLevel(logging.WARNING)
logging.getLogger('filelock').setLevel(logging.WARNING)
logging.getLogger('list_processor.autotrader_client').setLevel(logging.WARNING)
logging.getLogger('list_processor.cap_client').setLevel(logging.WARNING)

# Store SharePoint-file lines, warnings and errors in Aurora for the Logs page.
# Must come after basicConfig, which clears existing handlers.
from utils import app_log
app_log.install()


class BasePathMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Re-add the base path to URL path
        new_path = re.sub(r'^/dev', '', request.url.path)
        request.scope['path'] = new_path
        # Page and endpoint for the Logs page; read by anything logged while
        # this request runs.
        app_log.begin_request(new_path)
        started = time.monotonic()
        response = await call_next(request)
        # Slow requests go on the Logs page with their total time. Run in a
        # worker thread so the Aurora write does not hold up the event loop.
        if not new_path.startswith('/static'):
            await run_in_threadpool(app_log.record_request, request.method, new_path,
                                    response.status_code, int((time.monotonic() - started) * 1000))
        return response


# Add middleware for session management
_is_local = __name__ == '__main__' or os.getenv('LOCAL_ENV') == '1'
app.add_middleware(SessionMiddleware, secret_key="as#$RD$26xc04$GF3r1dFDmhjd", max_age=30 * 24 * 3600, https_only=not _is_local, same_site="none" if not _is_local else "lax")
# Add middleware for base path
app.add_middleware(BasePathMiddleware)
# Compresses any response over 1KB whose client sends Accept-Encoding: gzip
# (every browser does). Large JSON payloads (repeated keys, short numeric
# values) compress especially well — this is what keeps big API responses,
# e.g. Orders & Deliveries' per-vehicle data, under the Lambda/API Gateway 6MB
# synchronous response limit. Must be registered before Mangum wraps the app
# (it wraps `app` as a whole, so middleware order here still applies).
app.add_middleware(GZipMiddleware, minimum_size=1024)

# OAuth2 password bearer token utilization (assuming token-based auth for simplicity)
#oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


@app.get('/healthcheck')
def simple_healthcheck(request: Request):
    logger.info('Healthcheck')
    context = None
    if 'aws.context' in request.scope:
        context = request.scope['aws.context']
    return {"status": "ok", 'context': json.dumps(context, default=str)}
#
# @app.get("/", response_class=HTMLResponse)
# def login(request: Request):
#     return templates.TemplateResponse("login.html", {"request": request})
#
# @app.get('/healthcheck')
# def simple_healthcheck(request: Request):
#     logger.info('Healthcheck')
#     context = None
#     if 'aws.context' in request.scope:
#         context = request.scope['aws.context']
#     return {"status": "ok", 'context': json.dumps(context, default=str)}



@app.get("/test")
def read_root(request: Request):
    res = {"Hello": "World2"}

    # This is an asynchronous call that will be executed in a new AWS Lambda function context (as it was an intependent HTTP request, but not going through the API Gateway)
    lambda_requests_manager.simulate_http(
        aws_context=request.scope["aws.context"],
        path="/test_execution",
        method="POST",
        data=res
    )

    # Return the response to the client
    return res

@app.get("/test-secrets-manager")
def test_secrets_manager(request: Request):
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    varname = f'{env}/common/car_pricer/rds/admin_credentials'
    data = get_secret(varname)
    return {"status": "ok", 'data': data}


@app.get("/test-db")
def test_db(request: Request):
    """this function tries to establish a connection to amazon aurora db (postgresql)"""
    import psycopg2
    import psycopg2.extras

    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    varname = f'{env}/common/car_pricer/rds/admin_credentials'
    data = get_secret(varname)

    # Connect to the database
    connection = psycopg2.connect(
        host=data['cluster_endpoint'],
        user=data['username'],
        password=data['password'],
        database='postgres',
        port=5432
    )

    with connection.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
        # Create a new record
        sql = "SELECT * FROM test_table"
        cursor.execute(sql)
        result = cursor.fetchall()
        print(result)

    connection.close()

    return {"status": "ok", 'data': result}



@app.post("/test_execution")
def test_execution(data: dict):
    print('Test execution which can take up to 15 minutes to complete (max aws lambda timeout)')
    print(data)
    return {}


app.include_router(routers.router)


async def page_redirect_handler(request: Request, exc: PageRedirect):
    """A page route denied access. Registered BEFORE the HTTPException handler
    below, which renders error.html for anything it catches — a redirect must
    not go through it."""
    return RedirectResponse(url=exc.location, status_code=303)


app.add_exception_handler(PageRedirect, page_redirect_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "icon.png"))

handler = Mangum(app, lifespan="auto", api_gateway_base_path="/dev")
lambda_requests_manager = LambdaRequestsManager()
lambda_requests_manager.init(mangum_handler=handler)


# if there's a var LOCAL_ENV=1, run local http server
if __name__ == '__main__':
    import uvicorn
    print('Running local server')
    uvicorn.run('main:app', host="127.0.0.1", port=8080, reload=True, workers=1)
