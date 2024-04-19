import logging
import os

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_401_UNAUTHORIZED
from mangum import Mangum
import json
import hashlib

from utils.lambda_http import LambdaRequestsManager


app = FastAPI()
cur_dir = os.path.dirname(__file__)
templates_dir = os.path.join(cur_dir, "templates")
templates = Jinja2Templates(directory=templates_dir)

logger = logging.getLogger(__name__)

logging.basicConfig(level="INFO", format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
# redirect output to stdout
logger.addHandler(logging.StreamHandler())


# Add middleware for session management
app.add_middleware(SessionMiddleware, secret_key="as#$RD$26xc04$GF3r1dFDmhjd")

# OAuth2 password bearer token utilization (assuming token-based auth for simplicity)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

def get_current_user(request: Request):
    user = request.session.get('user')
    if not user:
        return None
        # raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Unauthorized", headers={"Location": "/login"})
        # redirect to login page
        # return RedirectResponse(url="/login", status_code=303)
    return user

@app.get("/", response_class=HTMLResponse)
def root(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user})

@app.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    # calculate sha256 hash of the password
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    if pwd_hash == "a05ef593239b0ff678c3bb69731e785c470d781a121692302912550aa2459b74":
        request.session['user'] = username
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"})

@app.get("/logout")
def logout(request: Request):
    request.session.pop('user', None)
    return RedirectResponse(url="/login", status_code=303)

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
    import boto3
    ssm = boto3.client('secretsmanager', region_name='eu-west-1')
    varname = 'prod/common/car_pricer/rds/admin_credentials'
    response = ssm.get_secret_value(SecretId=varname)
    data = json.loads(response['SecretString'])
    logger.info('Testing secrets manager')
    return {"status": "ok", 'data': data}


@app.get("/test-db")
def test_db(request: Request):
    """this function tries to establish a connection to amazon aurora db (mysql)"""
    import pymysql
    # Connect to the database
    connection = pymysql.connect(
        host='car-pricer-rds-cluster-prod.cluster-c2qnpexfpjx7.eu-west-1.rds.amazonaws.com',
        user='test',
        password='test',
        database='test',
        port=3306
    )

    with connection.cursor() as cursor:
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


handler = Mangum(app, lifespan="auto")
lambda_requests_manager = LambdaRequestsManager(mangum_handler=handler)


# if there's a var LOCAL_ENV=1, run local http server
if __name__ == '__main__':
    import uvicorn
    print('Running local server')
    uvicorn.run('main:app', host="0.0.0.0", port=8000, reload=True, workers=1)
