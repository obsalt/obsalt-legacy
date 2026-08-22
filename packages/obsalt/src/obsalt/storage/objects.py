"""Object storage. Production uses S3/MinIO. Raw blobs are unredacted."""

from __future__ import annotations

from typing import Any


class S3Objects:
    def __init__(self, settings: Any) -> None:
        import boto3
        from botocore.client import Config
        from botocore.exceptions import ClientError

        self._ClientError = ClientError
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}),
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            self.client.create_bucket(Bucket=self.bucket)

    def put(self, key: str, body: bytes, *, headers: dict[str, str]) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            Metadata={k.replace("_", "-")[:50]: v[:256] for k, v in headers.items()},
        )

    def get(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)
