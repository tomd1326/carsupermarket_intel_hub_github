import logging
import os
import re
import time
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.gzip import GZipMiddleware
from mangum import Mangum
import json

from exceptions import unhandled_exception_handler, http_exception_handler
from utils.lambda_http import LambdaRequestsManager
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool
import routers
from routers.core import PageRedirect, BASE_URL


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
logging.getLogger('clients.autotrader_client').setLevel(logging.WARNING)
logging.getLogger('clients.cap_client').setLevel(logging.WARNING)

# Store SharePoint-file lines, warnings and errors in Aurora for the Logs page.
# Must come after basicConfig, which clears existing handlers.
from utils import app_log
app_log.install()


class BasePathMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # With no base path (the uk-intel-hub domains), send old /dev/... page
        # links on to the same page without /dev. GET only: a redirect would
        # drop a POST's form data.
        if not BASE_URL and request.method == 'GET' and re.match(r'^/dev(/|$)', request.url.path):
            target = re.sub(r'^/dev', '', request.url.path) or '/'
            if request.url.query:
                target += '?' + request.url.query
            return RedirectResponse(url=target, status_code=301)
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


app.include_router(routers.router)


async def page_redirect_handler(request: Request, exc: PageRedirect):
    """A page route denied access. Registered BEFORE the HTTPException handler
    below, which renders error.html for anything it catches — a redirect must
    not go through it."""
    return RedirectResponse(url=exc.location, status_code=303)


app.add_exception_handler(PageRedirect, page_redirect_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)


class VersionedStaticFiles(StaticFiles):
    """Static files, with browsers told to keep versioned ones for a year.

    static_url() in routers/core.py adds ?v=<hash of the file's contents>, so a
    changed file always gets a new URL. That makes it safe for the browser to
    keep the old URL's copy without checking back — kpi.js alone is 362 KB, and
    it was being re-checked on every page load.

    Only URLs that carry a version are cached like this. A plain /static/...
    reference (the icon, fonts) has nothing to change when the file does, so it
    keeps the browser's normal behaviour. Errors are never cached.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        query = parse_qs(scope.get('query_string', b'').decode('latin-1'))
        if response.status_code == 200 and query.get('v'):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return response


app.mount("/static", VersionedStaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "icon.png"))

handler = Mangum(app, lifespan="auto", api_gateway_base_path=BASE_URL or "/")
lambda_requests_manager = LambdaRequestsManager()
lambda_requests_manager.init(mangum_handler=handler)


# if there's a var LOCAL_ENV=1, run local http server
if __name__ == '__main__':
    import uvicorn
    print('Running local server')
    uvicorn.run('main:app', host="127.0.0.1", port=8080, reload=True, workers=1)
