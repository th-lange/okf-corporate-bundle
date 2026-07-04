"""AWS S3 source connector (issue #18).

Third `Source` implementation: a configured bucket + prefix is enumerated,
`*.md` objects are downloaded, and the S3 ETag serves as the stable revision
id for the ingest ledger. With git, Drive, and S3 the connector seam is
proven: a new origin system is one class plus config.

The S3 API sits behind the tiny `S3Api` protocol so tests run against a fake
with no network and no AWS SDK. The real client uses boto3 (install the `s3`
extra) and therefore the standard AWS credential chain — env vars, profiles,
instance roles; credentials never live in ingest config files.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from okf_mcp.ingest.sources import SourceDocument, SourceError


class S3Api(Protocol):
    """The minimal S3 surface the connector needs."""

    def list_objects(self, bucket: str, prefix: str) -> list[dict]:
        """Object metadata dicts: key, etag."""
        ...

    def get_object(self, bucket: str, key: str) -> str:
        """Download one object's content as text."""
        ...


@dataclass(frozen=True)
class S3Source:
    """Pull markdown documents from an S3 bucket/prefix."""

    name: str
    bucket: str
    prefix: str = ""
    api: S3Api | None = None  # injectable for tests; None → boto3 client

    def documents(self) -> Iterator[SourceDocument]:
        api = self.api or Boto3S3Api.from_default_chain()
        for obj in api.list_objects(self.bucket, self.prefix):
            key = obj["key"]
            if not key.endswith(".md"):
                continue
            etag = str(obj.get("etag", "")).strip('"')
            if not etag:
                raise SourceError(f"S3 object {key!r} has no ETag to use as revision")
            relative_path = key[len(self.prefix) :].lstrip("/") if self.prefix else key
            yield SourceDocument(
                source_uri=f"s3://{self.bucket}/{key}",
                relative_path=relative_path,
                revision=etag,
                content=api.get_object(self.bucket, key),
            )


class Boto3S3Api:
    """Real S3 client via boto3 and the standard AWS credential chain."""

    def __init__(self, client: object) -> None:
        self._client = client

    @classmethod
    def from_default_chain(cls) -> Boto3S3Api:
        try:
            import boto3
        except ImportError:
            raise SourceError(
                "S3 sources need boto3 — install the `s3` extra "
                "(uv sync --extra s3). Credentials come from the standard "
                "AWS chain (env vars, profile, instance role)."
            ) from None
        return cls(boto3.client("s3"))

    def list_objects(self, bucket: str, prefix: str) -> list[dict]:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            paginator = self._client.get_paginator("list_objects_v2")
            return [
                {"key": obj["Key"], "etag": obj["ETag"]}
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
                for obj in page.get("Contents", [])
            ]
        except (BotoCoreError, ClientError) as exc:
            raise SourceError(f"S3 list failed for s3://{bucket}/{prefix}: {exc}") from exc

    def get_object(self, bucket: str, key: str) -> str:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
            return response["Body"].read().decode("utf-8")
        except (BotoCoreError, ClientError) as exc:
            raise SourceError(f"S3 get failed for s3://{bucket}/{key}: {exc}") from exc
