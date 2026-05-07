"""Set required env vars before the dashboard's modules are imported.

The webhook module instantiates Powertools'
``DynamoDBPersistenceLayer`` at module load (matching the documented
pattern for the idempotent-function decorator), so the table name has
to be in the environment before pytest collects the test modules.
"""

from __future__ import annotations

import os

os.environ.setdefault("AIDLC_IDEMPOTENCY_TABLE", "ai-dlc-test-idempotency")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
