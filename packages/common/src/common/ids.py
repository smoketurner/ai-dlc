"""ID helpers — UUID7 for time-sortable identifiers.

UUID7 is the right choice for run/event/correlation IDs because it sorts
chronologically (DynamoDB sort keys, S3 prefixes, log timelines) while still
being globally unique. We use the ``uuid-utils`` package to avoid pulling in
the standard library's ``uuid`` 4-only generator.
"""

from __future__ import annotations

from typing import NewType

import uuid_utils

# Distinct types so accidentally passing a run id where an event id is expected
# fails at type-check time. They're all backed by ``str``.
RunId = NewType("RunId", str)
EventId = NewType("EventId", str)
SessionId = NewType("SessionId", str)
CorrelationId = NewType("CorrelationId", str)
ApprovalId = NewType("ApprovalId", str)


def _uuid7() -> str:
    """Return a fresh UUID7 as a hyphenated string."""
    return str(uuid_utils.uuid7())


def new_run_id() -> RunId:
    """Generate a new run identifier."""
    return RunId(_uuid7())


def new_event_id() -> EventId:
    """Generate a new event identifier."""
    return EventId(_uuid7())


def new_session_id(*, agent_name: str, run_id: RunId) -> SessionId:
    """Build a deterministic session id from an agent name and run id.

    Step Functions state-machine input + agent name determines the AgentCore
    runtime session id, so the agent's persistent filesystem is reused on
    resume. The agent name keeps sessions for the architect and implementer
    distinct within the same run.

    Args:
        agent_name: The specialist agent name (e.g., ``"architect"``).
        run_id: The owning run.
    """
    return SessionId(f"{run_id}-{agent_name}")


def new_correlation_id() -> CorrelationId:
    """Generate a new correlation id (threads through events end-to-end)."""
    return CorrelationId(_uuid7())


def new_approval_id() -> ApprovalId:
    """Generate a new HITL approval id."""
    return ApprovalId(_uuid7())


def short_run_id(run_id: str) -> str:
    """Return the leading time-prefix of a UUID7 — branch-name-safe.

    Used to scope task branches per run (``aidlc/{slug}/{short}/{task_id}``)
    so concurrent or successive runs on the same spec don't share task
    branches. Spec branches use ``aidlc/spec/{slug}`` directly — only
    one in-flight spec per slug, so iteration commits land on the same
    branch and the existing PR auto-updates. UUID7's first 13 characters
    (``019e0e69-198d``) carry the millisecond timestamp, which is
    unique-enough across runs minted seconds apart and stays stable for
    the lifetime of one run.
    """
    return run_id.lower()[:13]
