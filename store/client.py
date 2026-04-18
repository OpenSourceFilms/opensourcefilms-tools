import logging
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from config.settings import settings

logger = logging.getLogger(__name__)

BUCKETS = ["media-originals", "media-proxies", "media-keyframes", "exports"]


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}",
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def ensure_buckets():
    s3 = _s3()
    for bucket in BUCKETS:
        try:
            s3.head_bucket(Bucket=bucket)
            logger.info("bucket exists: %s", bucket)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchBucket"):
                s3.create_bucket(Bucket=bucket)
                logger.info("created bucket: %s", bucket)
            else:
                raise


def upload_file(local_path: str, bucket: str, key: str) -> str:
    s3 = _s3()
    s3.upload_file(local_path, bucket, key)
    url = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}/{bucket}/{key}"
    logger.debug("uploaded %s → %s/%s", local_path, bucket, key)
    return url


def download_file(bucket: str, key: str, local_path: str):
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    s3 = _s3()
    s3.download_file(bucket, key, local_path)
    logger.debug("downloaded %s/%s → %s", bucket, key, local_path)


def get_presigned_url(bucket: str, key: str, expiry_seconds: int = 3600) -> str:
    s3 = _s3()
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expiry_seconds,
    )
    return url


def delete_object(bucket: str, key: str):
    s3 = _s3()
    s3.delete_object(Bucket=bucket, Key=key)
    logger.debug("deleted %s/%s", bucket, key)
