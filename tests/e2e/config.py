"""Smoke test configuration loaded from environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SmokeTestConfig(BaseSettings):
    """Configuration for the end-to-end smoke test suite."""

    ingest_endpoint: str = Field(
        default="http://localhost:8080/ingest",
        description="HTTP(S) URL of the pipeline ingest endpoint.",
    )
    delivery_sink_arn: str = Field(
        default="",
        description="ARN of the delivery sink (S3 bucket or SQS queue) to poll.",
    )
    aws_region: str = Field(
        default="us-east-1",
        description="AWS region used for boto3 clients.",
    )
    timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Total budget in seconds for the full smoke test run.",
    )

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=False,
        frozen=True,
        extra="ignore",
    )
