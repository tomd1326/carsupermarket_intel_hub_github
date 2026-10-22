from typing import Optional

import boto3
import json
import os
import asyncio
import logging
import uuid

from mangum import Mangum
from mangum.types import LambdaContext
from utils.singleton import Singleton


logger = logging.getLogger(__name__)


class LambdaRequestsManager(metaclass=Singleton):
    def __init__(self):
        self.mangum_handler = None
        self.lambda_client = None
        self.region_name = None

    def init(self, mangum_handler: Mangum, region_name: Optional[str] = None):
        if region_name is not None:
            self.region_name = region_name
        else:
            self.region_name = os.environ.get('AWS_REGION', 'eu-west-1')

        self.lambda_client = boto3.client('lambda', region_name=self.region_name)
        # self.function_name = function_name
        self.mangum_handler = mangum_handler

    def simulate_http(self, aws_context: LambdaContext, path, method, headers=None, params=None, data=None, asynchronous=True):
        # Construct the event object that mimics API Gateway
        event = {
            "httpMethod": method,
            "path": path,
            "headers": headers if headers else {},
            "multiValueHeaders": {},
            "queryStringParameters": params if params else {},
            "multiValueQueryStringParameters": {},
            "pathParameters": {},
            "stageVariables": {},
            "version": "2.0",
            "requestContext": {
                "accountId": "123456789012",
                "resourceId": "123456",
                "stage": "prod",
                "requestId": str(uuid.uuid4()),
                "identity": {
                    "sourceIp": "127.0.0.1"
                },
                "resourcePath": path,
                "httpMethod": method,
                "apiId": "1234567890",
                "operationName": path.strip('/'),
                "http": {
                    "method": method,
                    "path": path,
                    "protocol": "HTTP/1.1",
                    "sourceIp": "127.0.0.1"
                }
            },
            "body": json.dumps(data) if data else None,
            "isBase64Encoded": False
        }

        # Log the route only, never the event itself. The event embeds the posted
        # body and the headers, and both these lines reach CloudWatch under
        # Lambda — anything secret in a dispatched payload (e.g. the worker
        # credential on /keeper-change-check/run) would be written out in clear
        # on every call. The full event is available locally for debugging.
        logger.info(f"Simulating HTTP request: {method} {path}")
        if os.getenv('LOCAL_ENV') == '1':
            logger.debug(f"Simulated event: {event}")

        # If we are locally testing the Lambda function, we can invoke it directly
        if os.getenv('LOCAL_ENV') == '1' or aws_context is None:
            logger.info('Running locally')
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            resp = self.mangum_handler(event, aws_context)
            return resp

        # Otherwise, we need to invoke the Lambda function remotely

        invocation_type = 'RequestResponse'
        if asynchronous:
            invocation_type = 'Event'

        logger.info(f"Invoking ({invocation_type}) Lambda function {aws_context.function_name}: {method} {path}")

        # Invoke the Lambda function directly
        response = self.lambda_client.invoke(
            FunctionName=aws_context.function_name,
            InvocationType=invocation_type,
            Payload=json.dumps(event)
        )

        # Read and return the Lambda response
        if asynchronous:
            return response

        response_payload = response['Payload'].read()
        return json.loads(response_payload)
