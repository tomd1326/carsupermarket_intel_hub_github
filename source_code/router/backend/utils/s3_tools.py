import boto3


def upload_file(local_file_path: str, bucket_name: str, s3_file_path: str):
    """
    Upload a file to an S3 bucket

    :param local_file_path: The local path of the file
    :param bucket_name: The name of the S3 bucket
    :param s3_file_path: The path of the file in the S3 bucket
    """
    s3 = boto3.client('s3')
    s3.upload_file(local_file_path, bucket_name, s3_file_path)
    return True


def download_file(local_file_path: str, bucket_name: str, s3_file_path: str):
    """
    Download a file from an S3 bucket

    :param local_file_path: The local path to save the file
    :param bucket_name: The name of the S3 bucket
    :param s3_file_path: The path of the file in the S3 bucket
    """
    s3 = boto3.client('s3')
    s3.download_file(bucket_name, s3_file_path, local_file_path)
    return True
