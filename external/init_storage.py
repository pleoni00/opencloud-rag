import os
import time
import boto3
from botocore.exceptions import ClientError, EndpointConnectionError

client = boto3.client('s3', endpoint_url='http://minio:9000', region_name='us-east-1',
                      aws_access_key_id=os.environ['MINIO_ROOT_USER'],
                      aws_secret_access_key=os.environ['MINIO_ROOT_PASSWORD'])
for attempt in range(60):
    try:
        client.list_buckets()
        break
    except EndpointConnectionError:
        time.sleep(2)
else:
    raise RuntimeError('MinIO did not become ready')
try:
    client.head_bucket(Bucket='opencloud-documents')
except ClientError as exc:
    if exc.response['Error']['Code'] not in ('404', 'NoSuchBucket'):
        raise
    client.create_bucket(Bucket='opencloud-documents')
print('Private document bucket ready.')
