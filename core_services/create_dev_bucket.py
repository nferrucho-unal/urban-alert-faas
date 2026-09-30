import logging
import os
import time

from botocore.exceptions import ClientError

from core_services.storage import s3_bucket, s3_client

LOGGER = logging.getLogger("s3-local-bootstrap")


def main():
    client = s3_client()
    bucket = s3_bucket()
    last_error = None
    for _ in range(30):
        try:
            client.head_bucket(Bucket=bucket)
            LOGGER.info("S3 bucket %s ya existe", bucket)
            return
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchBucket", "NotFound"}:
                try:
                    client.create_bucket(Bucket=bucket)
                    LOGGER.info("S3 bucket %s creado", bucket)
                    return
                except ClientError as create_error:
                    if create_error.response.get("Error", {}).get("Code") not in {
                        "BucketAlreadyExists",
                        "BucketAlreadyOwnedByYou",
                    }:
                        last_error = create_error
            else:
                last_error = error
        except Exception as error:
            last_error = error
        time.sleep(2)
    raise RuntimeError(f"No se pudo inicializar el bucket local {bucket}: {last_error}")


if __name__ == "__main__":
    main()
