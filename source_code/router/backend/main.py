import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_401_UNAUTHORIZED
from mangum import Mangum
import json
import hashlib

from exceptions import unhandled_exception_handler
from utils.secrets_manager_client import get_secret
from utils.lambda_http import LambdaRequestsManager
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Application starting up...")
    yield
    # Shutdown
    logger.info("Application shutting down...")


app = FastAPI(lifespan=lifespan)

logger = logging.getLogger(__name__)

# Force configure logging for AWS Lambda
logging.getLogger().handlers.clear()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", force=True)

# Disable useless logging
logging.getLogger('botocore').setLevel(logging.CRITICAL)
logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('s3transfer').setLevel(logging.CRITICAL)
logging.getLogger('urllib3').setLevel(logging.CRITICAL)
logging.getLogger('asyncio').setLevel(logging.CRITICAL)
logging.getLogger('multipart').setLevel(logging.CRITICAL)
logging.getLogger('python_multipart').setLevel(logging.WARNING)  # Add this
logging.getLogger('utils.secrets_manager_client').setLevel(logging.WARNING)  # Add this
logging.getLogger('list_processor.autotrader_client').setLevel(logging.WARNING)  # Add this
logging.getLogger('list_processor.cap_client').setLevel(logging.WARNING)  # Add this
logging.getLogger('utils.s3_tools').setLevel(logging.WARNING)  # Add this


class BasePathMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Re-add the base path to URL path
        new_path = re.sub(r'^/dev', '', request.url.path)
        request.scope['path'] = new_path
        response = await call_next(request)
        return response


# Add middleware for session management
app.add_middleware(SessionMiddleware, secret_key="as#$RD$26xc04$GF3r1dFDmhjd")
# Add middleware for base path
app.add_middleware(BasePathMiddleware)


@app.get('/healthcheck')
def simple_healthcheck(request: Request):
    logger.info('Healthcheck')
    context = None
    if 'aws.context' in request.scope:
        context = request.scope['aws.context']
    return {"status": "ok", 'context': json.dumps(context, default=str)}


@app.get("/test")
def read_root(request: Request):
    res = {"Hello": "World2"}

    # This is an asynchronous call that will be executed in a new AWS Lambda function context
    lambda_requests_manager.simulate_http(
        aws_context=request.scope["aws.context"],
        path="/test_execution",
        method="POST",
        data=res
    )

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


app.include_router(router.router)
app.add_exception_handler(Exception, unhandled_exception_handler)

# TODO: probably it's more correct to put api_gateway_base_path="/dev" here
handler = Mangum(app, lifespan="auto")
lambda_requests_manager = LambdaRequestsManager()
lambda_requests_manager.init(mangum_handler=handler)


# if there's a var LOCAL_ENV=1, run local http server
if __name__ == '__main__':
    import uvicorn
    print('Running local server')
    uvicorn.run('main:app', host="0.0.0.0", port=8080, reload=True, workers=1)