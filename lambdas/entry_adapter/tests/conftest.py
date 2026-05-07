"""Set required env vars before the handler module is imported.

The handler builds Powertools' ``DynamoDBPersistenceLayer`` at module load
time (matching the documented pattern), so the table name has to be in the
environment before pytest collects the test modules.
"""

from __future__ import annotations

import os

os.environ.setdefault("AIDLC_IDEMPOTENCY_TABLE", "ai-dlc-test-idempotency")
os.environ.setdefault("AIDLC_BUS_NAME", "ai-dlc-test-bus")
os.environ.setdefault("AIDLC_RUNS_TABLE", "ai-dlc-test-runs")
os.environ.setdefault(
    "AIDLC_BEACON_QUEUE_URL",
    "https://sqs.us-east-1.amazonaws.com/000000000000/ai-dlc-test-state-router",
)
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
