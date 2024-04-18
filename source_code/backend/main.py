import logging
from fastapi import FastAPI, Request
from mangum import Mangum

from utils.lambda_http import LambdaRequestsManager

app = FastAPI()
logger = logging.getLogger(__name__)

logging.basicConfig(level="INFO", format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


@app.get('/')
def simple_healthcheck(request: Request):
    logger.info('Healthcheck')
    return {"status": "ok"}


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


@app.post("/test_execution")
def test_execution(data: dict):
    print('Test execution which can take up to 15 minutes to complete (max aws lambda timeout)')
    print(data)
    return {}


handler = Mangum(app, lifespan="auto")
lambda_requests_manager = LambdaRequestsManager(mangum_handler=handler)

