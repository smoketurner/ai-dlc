"""Helpers for the AgentCore Runtime contract.

Both agents serve HTTP on ``:8080`` and expose ``POST /invocations`` and
``GET /ping``. The ``bedrock-agentcore`` SDK ships :class:`BedrockAgentCoreApp`
that handles the contract for us — we just supply an entrypoint coroutine.

This module collects the small shared scaffolding (typed payload model,
session-id parsing) so each agent's ``app.py`` stays under 80 lines.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class InvocationPayload(BaseModel):
    """Validated input passed to every agent ``/invocations`` call.

    Step Functions sends this body when invoking the agent runtime. Every
    field is required — the agent fails fast if Step Functions or a manual
    invocation omits one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    project_slug: Annotated[str, Field(min_length=1, max_length=64)]
    intent: Annotated[str, Field(min_length=1, max_length=4096)]
    run_id: str
    correlation_id: str
    actor_id: str = "system"
    adr_s3_key: str | None = None  # populated for the Implementer
