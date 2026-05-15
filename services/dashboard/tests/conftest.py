"""Set required env vars before the dashboard's modules are imported.

The webhook module instantiates Powertools'
``DynamoDBPersistenceLayer`` at module load (matching the documented
pattern for the idempotent-function decorator), so the table name has
to be in the environment before pytest collects the test modules.

``dashboard.app`` registers ``SessionMiddleware`` at import time, which
in turn calls :func:`dashboard.auth.session_secret` — that needs a
fully populated :class:`dashboard.deps.Settings` to instantiate. We
seed every required env var with a placeholder here so per-test
fixtures can monkeypatch only the values they care about.
"""

from __future__ import annotations

import os

os.environ.setdefault("AIDLC_AUTH", "disabled")
os.environ.setdefault("AIDLC_ENV", "dev")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AIDLC_BUS_NAME", "ai-dlc-test-bus")
os.environ.setdefault("AIDLC_RUNS_TABLE", "ai-dlc-test-runs")
os.environ.setdefault("AIDLC_IDEMPOTENCY_TABLE", "ai-dlc-test-idempotency")
os.environ.setdefault("AIDLC_ARTIFACTS_BUCKET", "ai-dlc-test-artifacts")
os.environ.setdefault(
    "AIDLC_GITHUB_APP_SECRET_ARN",
    "arn:aws:secretsmanager:us-east-1:000000000000:secret:test-app",
)
os.environ.setdefault("AIDLC_GITHUB_WEBHOOK_SECRET_ID", "test-webhook-secret")
os.environ.setdefault("AIDLC_COGNITO_USER_POOL_ID", "us-east-1_TEST")
os.environ.setdefault("AIDLC_COGNITO_CLIENT_ID", "test-client")
