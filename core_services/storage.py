import os

import boto3
from botocore.config import Config


MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(15 * 1024 * 1024)))
UPLOAD_URL_TTL_SECONDS = int(os.environ.get("UPLOAD_URL_TTL_SECONDS", "300"))
DOWNLOAD_URL_TTL_SECONDS = int(os.environ.get("DOWNLOAD_URL_TTL_SECONDS", "300"))
ALLOWED_MIME_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def s3_bucket():
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("Falta configurar S3_BUCKET.")
    return bucket


def s3_client(for_presigning=False):
    kwargs = {
        "region_name": os.environ.get("AWS_REGION", "us-east-1"),
        "config": Config(
            signature_version="s3v4",
            connect_timeout=3,
            read_timeout=5,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    }
    endpoint_variable = (
        "S3_PRESIGN_ENDPOINT_URL" if for_presigning else "S3_ENDPOINT_URL"
    )
    endpoint_url = os.environ.get(endpoint_variable) or os.environ.get("S3_ENDPOINT_URL")
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    access_key = os.environ.get("S3_ACCESS_KEY_ID")
    secret_key = os.environ.get("S3_SECRET_ACCESS_KEY")
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
    return boto3.client("s3", **kwargs)
