"""Smoke test configuration loaded from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class SmokeTestConfig(BaseSettings):
    """Environment-driven configuration for the marketplace smoke test."""

    aws_region: str
    product_id: str
    endpoint_url: str
    timeout_seconds: int = 300
    teardown_on_failure: bool = True

    model_config = SettingsConfigDict(
        env_prefix="SMOKE_",
        env_file=None,
        case_sensitive=False,
        frozen=True,
        extra="ignore",
    )
