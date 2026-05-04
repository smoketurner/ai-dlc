"""Bridge SNS alarm notifications to the Proposer AgentCore Runtime.

The drift_detector emits a ``RegressionDetected`` metric; the
``${prefix}-eval-regression`` alarm fires on it and notifies the alerts
SNS topic. This Lambda subscribes to that topic, filters for "regression"
messages, and invokes the Proposer runtime with
``trigger_reason="regression"``. Anything else (OK transitions, unrelated
alerts) is ignored — keeps the bridge cheap and one-purpose.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import boto3

agentcore = boto3.client("bedrock-agentcore")


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """SNS event handler. Each Records entry is one SNS message."""
    runtime_arn = os.environ["AIDLC_PROPOSER_RUNTIME_ARN"]
    project_slug = os.environ["AIDLC_PROJECT_SLUG"]
    target_repo = os.environ["AIDLC_TARGET_REPO"]
    lookback_days = int(os.environ.get("AIDLC_LOOKBACK_DAYS", "30"))

    invocations = 0
    for record in event.get("Records", []):
        sns = record.get("Sns") or {}
        message = sns.get("Message", "") or ""
        # The drift_detector publishes a multiline message starting with
        # "ai-dlc eval pass-rate regression detected"; the alarm itself
        # publishes JSON-shaped CloudWatch alarm payloads. We act on either.
        if not is_regression_signal(message):
            continue
        run_id = f"proposer-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        agentcore.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier="DEFAULT",
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(
                {
                    "project_slug": project_slug,
                    "target_repo": target_repo,
                    "base_branch": "main",
                    "trigger_reason": "regression",
                    "evals_lookback_days": lookback_days,
                    "run_id": run_id,
                    "correlation_id": run_id,
                    "actor_id": "regression-trigger",
                },
            ).encode("utf-8"),
        )
        invocations += 1
    return {"ok": True, "invocations": invocations}


def is_regression_signal(message: str) -> bool:
    """Return True if the SNS message indicates a regression (not an OK transition)."""
    if "ai-dlc eval pass-rate regression detected" in message:
        return True
    # CloudWatch alarm payloads — JSON-shaped. Look for the ALARM state.
    try:
        parsed = json.loads(message)
    except ValueError, TypeError:
        return False
    if not isinstance(parsed, dict):
        return False
    return parsed.get("NewStateValue") == "ALARM"
