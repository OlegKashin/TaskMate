import hashlib
import io
import time
import uuid
from pathlib import Path

import boto3
from botocore.config import Config

from app.core.config import get_settings


class S3Storage:
    """S3 adapter pointed at the local SeaweedFS gateway by default."""

    def __init__(self, client=None):
        settings = get_settings()
        self.bucket = settings.s3_bucket
        self.client = client or boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(s3={"addressing_style": "path"}),
        )

    def ensure_bucket(self):
        buckets = {item["Name"] for item in self.client.list_buckets().get("Buckets", [])}
        if self.bucket not in buckets:
            self.client.create_bucket(Bucket=self.bucket)

    def put(
        self,
        user_id: uuid.UUID,
        message_id: uuid.UUID,
        attachment_id: uuid.UUID,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
    ):
        extension = Path(filename).suffix
        key = f"users/{user_id}/messages/{message_id}/{attachment_id}{extension}"
        self.client.upload_fileobj(
            io.BytesIO(content),
            self.bucket,
            key,
            ExtraArgs={"ContentType": mime_type or "application/octet-stream"},
        )
        return key, hashlib.sha256(content).hexdigest()

    def signed_url(self, key: str, expires=900) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires
        )


def initialize_local_bucket(attempts: int = 20, delay_seconds: float = 1.0):
    storage = S3Storage()
    for attempt in range(attempts):
        try:
            storage.ensure_bucket()
            return
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(delay_seconds)


if __name__ == "__main__":  # pragma: no cover - container startup command
    initialize_local_bucket()
