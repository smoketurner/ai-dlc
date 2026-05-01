"""S3 artifact I/O — typed wrappers around boto3.

Two buckets:

* ``artifacts`` — specs, ADRs, generated code, test reports.
* ``memory_md`` — per-project ``MEMORY.md`` snapshots.

Keep this module deliberately small: just put/get/list with structured errors.
Anything more elaborate (presigned URLs, multipart uploads) belongs in callers
or its own module.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, cast

from botocore.exceptions import BotoCoreError, ClientError

from common.errors import S3ArtifactError

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_s3.type_defs import PaginatorConfigTypeDef


def put_text(
    client: S3Client,
    /,
    *,
    bucket: str,
    key: str,
    body: str,
    content_type: str = "text/plain; charset=utf-8",
) -> None:
    """Upload UTF-8 text to ``s3://bucket/key``.

    Raises:
        S3ArtifactError: On any boto3 error.
    """
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType=content_type,
            ServerSideEncryption="aws:kms",
        )
    except (BotoCoreError, ClientError) as exc:
        raise S3ArtifactError("put_text failed", bucket=bucket, key=key) from exc


def get_text(client: S3Client, /, *, bucket: str, key: str) -> str:
    """Download a UTF-8 text object from ``s3://bucket/key``.

    Raises:
        S3ArtifactError: On any boto3 error.
    """
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        raw = response["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        raise S3ArtifactError("get_text failed", bucket=bucket, key=key) from exc
    return raw.decode("utf-8")


def list_keys(
    client: S3Client,
    /,
    *,
    bucket: str,
    prefix: str,
    page_size: int = 100,
) -> Iterator[str]:
    """Yield keys under ``s3://bucket/prefix``.

    Raises:
        S3ArtifactError: On any boto3 error.
    """
    paginator = client.get_paginator("list_objects_v2")
    pagination = cast("PaginatorConfigTypeDef", {"PageSize": page_size})
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, PaginationConfig=pagination):
            for obj in page.get("Contents", []):
                key = obj.get("Key")
                if key is not None:
                    yield key
    except (BotoCoreError, ClientError) as exc:
        raise S3ArtifactError("list_keys failed", bucket=bucket, prefix=prefix) from exc
