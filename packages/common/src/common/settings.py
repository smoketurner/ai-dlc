"""Runtime configuration for ai-dlc components.

All deployable units (agents, lambdas, dashboard) read configuration from
environment variables prefixed ``AIDLC_``. Values that come from SSM Parameter
Store are written into the env at deploy time by the task definition or Lambda
configuration; this module never reaches into SSM directly.

Frozen settings: instantiate once at import or cold-start time and inject the
instance — do not mutate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["dev", "staging", "prod"]


class Settings(BaseSettings):
    """Top-level configuration loaded from ``AIDLC_*`` environment variables."""

    env: Env = "dev"
    region: str = "us-east-1"

    # EventBridge + DynamoDB (resolved by the env's Terraform outputs)
    eventbridge_bus_name: str = Field(default="ai-dlc-bus-dev")
    runs_table: str = Field(default="ai-dlc-runs-dev")
    idempotency_table: str = Field(default="ai-dlc-idempotency-dev")
    state_router_queue_url: str | None = None

    # S3
    artifacts_bucket: str = Field(default="ai-dlc-artifacts-dev")
    memory_md_bucket: str = Field(default="ai-dlc-memory-md-dev")

    # AgentCore
    agentcore_memory_id: str | None = None
    agentcore_gateway_url: str | None = None
    architect_runtime_arn: str | None = None
    implementer_runtime_arn: str | None = None

    # Models — Bedrock cross-region inference profiles, resolved at apply time
    bedrock_model_architect: str = "us.anthropic.claude-opus-4-7-20260415-v1:0"
    bedrock_model_implementer: str = "us.anthropic.claude-sonnet-4-6-20260201-v1:0"
    bedrock_model_consolidation: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    # Auth
    cognito_user_pool_id: str | None = None
    cognito_app_client_id: str | None = None
    cognito_issuer_url: str | None = None

    # Behavior toggles
    log_level: str = "INFO"
    max_run_cost_usd: float = 5.0

    # OTEL
    otel_service_name: str = "ai-dlc"
    otel_exporter_otlp_endpoint: str | None = None

    model_config = SettingsConfigDict(
        env_prefix="AIDLC_",
        env_file=None,
        case_sensitive=False,
        frozen=True,
        extra="ignore",
    )
