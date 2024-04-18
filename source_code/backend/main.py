import logging
import os

from fastapi import FastAPI, Request
from mangum import Mangum
import json

from utils.lambda_http import LambdaRequestsManager


app = FastAPI()
logger = logging.getLogger(__name__)

logging.basicConfig(level="INFO", format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
# redirect output to stdout
logger.addHandler(logging.StreamHandler())


@app.get('/')
def simple_healthcheck(request: Request):
    logger.info('Healthcheck')
    return {"status": "ok", 'context': json.dumps(request.scope['aws.context'], default=str)}


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
    ssm = boto3.client('ssm')
    response = ssm.get_parameter(Name='prod/common/car_pricer/rds/admin_credentials', WithDecryption=True)
    data = response['Parameter']['Value']
    print(data)
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
    uvicorn.run(app, host="0.0.0.0", port=8000)
