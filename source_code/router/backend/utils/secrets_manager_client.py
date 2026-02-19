import json
import os
import logging
import boto3

logger = logging.getLogger(__name__)


def get_secret(secret_name: str) -> dict:
    sm = boto3.client(
        'secretsmanager',
        region_name=os.getenv('AWS_REGION', 'eu-west-1'),
        config=boto3.session.Config(
            connect_timeout=5,
            read_timeout=5,
            retries={'max_attempts': 0}
        )
    )
    logger.debug(f'Trying to get secret {secret_name}')

    response = sm.get_secret_value(SecretId=secret_name)
    data = json.loads(response['SecretString'])

    return data
