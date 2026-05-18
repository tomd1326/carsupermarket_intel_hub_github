"""
Recreate user_passwords.json in S3 for prod (old code still reads from S3).

Run from source_code/router/backend/:
    python tests/recreate_s3_passwords.py
"""

import os
import sys
import json
import boto3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

S3_BUCKET = "aa-raw-sources-uk"
S3_KEY    = "applications/pricing/uk-vehicle-list-evaluator/prod/user_passwords.json"

USERS = {
    'amartindale':  '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'aneedham':     '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'bcampbell':    '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'cbrown':       '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'dscott':       '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'dwilliams':    '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'ihigh':        '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'jjagger':      '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'jtaylor':      '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'kwalton':      '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'lrickles':     '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'mlewis':       '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'mteal':        '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'ovollans':     '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'rjagger':      '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'srichardson':  '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
    'tdrayson':     'e026581a53dc97e33815e606938c4b38b9428a0aac57a08bc4c1b3d1f3fb1b5c',
    'tlewis':       '23ba868e108988ab6c2c99aecac65bf16e615098c08e814b4bd113ff83b7fa44',
}

s3 = boto3.client('s3')
body = json.dumps(USERS, indent=2).encode('utf-8')
s3.put_object(Bucket=S3_BUCKET, Key=S3_KEY, Body=body, ContentType='application/json')
print(f"Uploaded {len(USERS)} users to s3://{S3_BUCKET}/{S3_KEY}")
