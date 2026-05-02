"""Helpers for the AgentCore Runtime contract.

Both agents serve HTTP on ``:8080`` and expose ``POST /invocations`` and
``GET /ping``. The ``bedrock-agentcore`` SDK ships :class:`BedrockAgentCoreApp`
that handles the contract for us — we just supply an entrypoint coroutine.

This module collects the small shared scaffolding — input/output models —
so each agent's ``app.py`` stays under 80 lines.

The pipeline is spec-driven:

  * The **Architect** receives an :class:`ArchitectInput` (intent + retry
    feedback) and returns an :class:`ArchitectResult` (spec_s3_prefix +
    summaries + task count).
  * The **Implementer** is invoked once per task and receives an
    :class:`ImplementerInput` (spec_slug + task_id + retry feedback),
    returning an :class:`ImplementerResult` (pr_url + diff_summary).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    """Strict, frozen base for the runtime contract types."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ArchitectInput(_Frozen):
    """Input passed to the Architect's ``/invocations`` endpoint.

    Step Functions sends this body when invoking the architect runtime,
    populating ``prior_feedback`` if this is a retry after rejection.
    """

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    intent: Annotated[str, Field(min_length=1, max_length=4096)]
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    prior_feedback: str | None = None


class ArchitectResult(_Frozen):
    """Result the Architect returns. Becomes the SPEC.READY payload."""

    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    spec_s3_prefix: str
    requirements_summary: Annotated[str, Field(max_length=1024)]
    design_summary: Annotated[str, Field(max_length=1024)]
    task_count: Annotated[int, Field(ge=1)]
    proposed_adrs: list[str] = Field(default_factory=list)
    session_id: str


class ImplementerInput(_Frozen):
    """Input passed to the Implementer's ``/invocations`` endpoint, per task."""

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    spec_slug: Annotated[str, Field(min_length=1, max_length=128)]
    spec_s3_prefix: str
    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    prior_feedback: str | None = None


class ImplementerResult(_Frozen):
    """Result the Implementer returns. Becomes the TASK.READY payload."""

    task_id: str
    pr_url: str
    diff_summary: Annotated[str, Field(max_length=4096)]
    session_id: str
