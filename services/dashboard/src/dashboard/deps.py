"""Shared dependencies — settings, boto3 clients, FastAPI dependencies."""

from __future__ import annotations

import os
from functools import cache
from typing import TYPE_CHECKING

import boto3
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_events.client import EventBridgeClient
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_s3.client import S3Client


class Settings(BaseModel):
    """Process-level settings, populated from environment variables."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    env: str
    region: str
    bus_name: str
    runs_table: str
    approvals_table: str
    idempotency_table: str
    artifacts_bucket: str
    hitl_handler_function: str
    triage_dispatcher_function: str
    iteration_reactor_function: str
    github_app_secret_id: str
    github_webhook_secret_id: str
    cognito_region: str
    cognito_user_pool_id: str
    cognito_client_id: str
    auth_disabled: bool
    dashboard_workload_name: str
    github_oauth_provider_name: str
    dashboard_oauth_return_url: str
    # Login of the GitHub bot the platform runs as. When set, an
    # ``issues.assigned`` webhook routes to triage if the new assignee
    # matches this login. Unset → assigned-trigger is disabled (only the
    # label-based + ``/aidlc go`` triggers fire).
    github_bot_login: str


@cache
def settings() -> Settings:
    """Process-cached :class:`Settings` from env."""
    return Settings(
        env=os.environ["AIDLC_ENV"],
        region=os.environ["AWS_REGION"],
        bus_name=os.environ["AIDLC_BUS_NAME"],
        runs_table=os.environ["AIDLC_RUNS_TABLE"],
        approvals_table=os.environ["AIDLC_APPROVALS_TABLE"],
        idempotency_table=os.environ["AIDLC_IDEMPOTENCY_TABLE"],
        artifacts_bucket=os.environ["AIDLC_ARTIFACTS_BUCKET"],
        hitl_handler_function=os.environ["AIDLC_HITL_HANDLER_FUNCTION"],
        triage_dispatcher_function=os.environ["AIDLC_TRIAGE_DISPATCHER_FUNCTION"],
        iteration_reactor_function=os.environ["AIDLC_ITERATION_REACTOR_FUNCTION"],
        github_app_secret_id=os.environ["AIDLC_GITHUB_APP_SECRET_ARN"],
        github_webhook_secret_id=os.environ["AIDLC_GITHUB_WEBHOOK_SECRET_ID"],
        cognito_region=os.environ.get("AIDLC_COGNITO_REGION", os.environ["AWS_REGION"]),
        cognito_user_pool_id=os.environ["AIDLC_COGNITO_USER_POOL_ID"],
        cognito_client_id=os.environ["AIDLC_COGNITO_CLIENT_ID"],
        auth_disabled=os.environ.get("AIDLC_AUTH", "enabled").lower() == "disabled",
        dashboard_workload_name=os.environ.get("AIDLC_DASHBOARD_WORKLOAD_NAME", ""),
        github_oauth_provider_name=os.environ.get("AIDLC_GITHUB_OAUTH_PROVIDER_NAME", ""),
        dashboard_oauth_return_url=os.environ.get("AIDLC_DASHBOARD_OAUTH_RETURN_URL", ""),
        github_bot_login=os.environ.get("AIDLC_GITHUB_BOT_LOGIN", ""),
    )


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb", region_name=settings().region)


@cache
def events() -> EventBridgeClient:
    """Process-cached EventBridge client."""
    return boto3.client("events", region_name=settings().region)


@cache
def lambda_client() -> LambdaClient:
    """Process-cached Lambda client."""
    return boto3.client("lambda", region_name=settings().region)


@cache
def s3() -> S3Client:
    """Process-cached S3 client."""
    return boto3.client("s3", region_name=settings().region)


@cache
def secrets() -> object:
    """Process-cached Secrets Manager client."""
    return boto3.client("secretsmanager", region_name=settings().region)
