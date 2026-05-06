"""Truncate dev DDB tables + clear synthetic-spec / run prefixes from S3.

One-shot operator script — run from a laptop with AWS creds. Wipes the
runs and approvals tables and clears ``specs/`` and ``runs/`` in the
artifacts bucket so the dashboard starts from an empty state. Prompts
for confirmation before each destructive action; pass ``--yes`` to skip.

Usage::

    uv run python scripts/cleanup_dev.py
    uv run python scripts/cleanup_dev.py --env dev --yes
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any

import boto3

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_s3.client import S3Client


PROJECT = "ai-dlc"


def parse_args() -> argparse.Namespace:
    """Parse CLI flags."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="dev", help="Environment suffix (default: dev)")
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region the dev account is in (default: us-east-1)",
    )
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    parser.add_argument(
        "--keep-s3",
        action="store_true",
        help="Truncate DDB only — leave S3 artifacts in place",
    )
    return parser.parse_args()


def confirm(prompt: str, *, skip: bool) -> bool:
    """Return True when the user types 'yes' (or ``--yes`` was passed)."""
    if skip:
        return True
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def truncate_table(ddb: DynamoDBClient, *, table: str) -> int:
    """Scan + batch-delete every row. Returns the number of rows removed."""
    paginator = ddb.get_paginator("scan")
    deleted = 0
    pending: list[dict[str, Any]] = []
    for page in paginator.paginate(
        TableName=table,
        ProjectionExpression="pk, sk",
    ):
        for item in page.get("Items", []):
            pending.append({"DeleteRequest": {"Key": {"pk": item["pk"], "sk": item["sk"]}}})
            if len(pending) == 25:  # noqa: PLR2004
                deleted += flush(ddb, table, pending)
                pending = []
    if pending:
        deleted += flush(ddb, table, pending)
    return deleted


def flush(ddb: DynamoDBClient, table: str, batch: list[dict[str, Any]]) -> int:
    """Send one batch_write_item; retry unprocessed items until none remain."""
    items = batch
    sent = 0
    while items:
        resp = ddb.batch_write_item(RequestItems={table: items})  # ty: ignore[invalid-argument-type]
        sent += len(items) - len(resp.get("UnprocessedItems", {}).get(table, []))
        items = resp.get("UnprocessedItems", {}).get(table, [])
    return sent


def truncate_idempotency(ddb: DynamoDBClient, *, table: str) -> int:
    """Idempotency table has a single hash key (no sort key)."""
    paginator = ddb.get_paginator("scan")
    deleted = 0
    pending: list[dict[str, Any]] = []
    for page in paginator.paginate(TableName=table, ProjectionExpression="idempotency_key"):
        for item in page.get("Items", []):
            pending.append(
                {"DeleteRequest": {"Key": {"idempotency_key": item["idempotency_key"]}}},
            )
            if len(pending) == 25:  # noqa: PLR2004
                deleted += flush(ddb, table, pending)
                pending = []
    if pending:
        deleted += flush(ddb, table, pending)
    return deleted


def clear_prefix(s3: S3Client, *, bucket: str, prefix: str) -> int:
    """Delete every object under ``prefix``. Returns the count removed."""
    paginator = s3.get_paginator("list_objects_v2")
    deleted = 0
    pending: list[dict[str, str]] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            pending.append({"Key": obj["Key"]})
            if len(pending) == 1000:  # noqa: PLR2004
                deleted += flush_s3(s3, bucket, pending)
                pending = []
    if pending:
        deleted += flush_s3(s3, bucket, pending)
    return deleted


def flush_s3(s3: S3Client, bucket: str, batch: list[dict[str, str]]) -> int:
    """Delete a batch of S3 objects."""
    resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})  # ty: ignore[invalid-argument-type]
    return len(resp.get("Deleted") or [])


def artifacts_bucket_name(s3: S3Client, *, env: str, region: str) -> str | None:
    """Find the artifacts bucket. The Terraform name embeds the account id.

    Lists buckets in the account and picks the one matching the
    project/env/region prefix. Returns ``None`` when no match — caller
    skips S3 cleanup.
    """
    prefix = f"{PROJECT}-{env}-artifacts-"
    suffix = f"-{region}"
    for bucket in s3.list_buckets().get("Buckets") or []:
        name = bucket["Name"]
        if name.startswith(prefix) and name.endswith(suffix):
            return name
    return None


def main() -> int:
    """Entrypoint."""
    args = parse_args()
    ddb = boto3.client("dynamodb", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)

    runs_table = f"{PROJECT}-{args.env}-runs"
    approvals_table = f"{PROJECT}-{args.env}-approvals"
    idempotency_table = f"{PROJECT}-{args.env}-idempotency-keys"

    if not confirm(
        f"Truncate {runs_table}, {approvals_table}, {idempotency_table}?",
        skip=args.yes,
    ):
        print("aborted", file=sys.stderr)  # noqa: T201
        return 1

    print(f"  {runs_table}: ", end="", flush=True)  # noqa: T201
    print(truncate_table(ddb, table=runs_table), "rows deleted")  # noqa: T201
    print(f"  {approvals_table}: ", end="", flush=True)  # noqa: T201
    print(truncate_table(ddb, table=approvals_table), "rows deleted")  # noqa: T201
    print(f"  {idempotency_table}: ", end="", flush=True)  # noqa: T201
    print(truncate_idempotency(ddb, table=idempotency_table), "rows deleted")  # noqa: T201

    if args.keep_s3:
        return 0

    bucket = artifacts_bucket_name(s3, env=args.env, region=args.region)
    if bucket is None:
        print(f"no artifacts bucket found for env={args.env} region={args.region}")  # noqa: T201
        return 0

    if not confirm(f"Clear specs/ and runs/ from s3://{bucket}/?", skip=args.yes):
        return 0

    print(f"  s3://{bucket}/specs/: ", end="", flush=True)  # noqa: T201
    print(clear_prefix(s3, bucket=bucket, prefix="specs/"), "objects deleted")  # noqa: T201
    print(f"  s3://{bucket}/runs/: ", end="", flush=True)  # noqa: T201
    print(clear_prefix(s3, bucket=bucket, prefix="runs/"), "objects deleted")  # noqa: T201

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
