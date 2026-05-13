"""Trace context propagation for ``invoke_agent_runtime`` calls.

The ``InvokeAgentRuntime`` API accepts ``traceId`` (X-Ray),
``traceParent`` / ``traceState`` (W3C trace context), and ``baggage``
(W3C baggage). When the caller threads them through, the agent's
OTEL spans continue the parent trace rather than starting a fresh
tree — which is what gives us a single view of the request path from
``entry_adapter`` → SQS beacon → ``state_router`` → AgentCore Runtime
→ ``event_projector``.

Inside an AWS Lambda, the runtime sets ``_X_AMZN_TRACE_ID`` to the
current X-Ray header (``Root=...;Parent=...;Sampled=1``). That string
is exactly what AgentCore expects in ``traceId``.

W3C ``traceparent`` is only included when the caller is OTEL-active
and provides a current span context; in the state-router today that's
not the case, so :func:`current_trace_context` returns ``traceId``
only and leaves the W3C slots empty. Adding W3C later is a no-op
expansion of the returned dict.
"""

from __future__ import annotations

import os


def current_trace_context() -> dict[str, str]:
    """Return the trace-related kwargs for ``invoke_agent_runtime``.

    Splat into the boto3 call::

        runtime_client().invoke_agent_runtime(
            agentRuntimeArn=...,
            **current_trace_context(),
            ...,
        )

    Empty dict when no trace is active (local pytest, etc.) — the
    boto3 call is fine with the parameters omitted.

    Returns:
        A mapping with at most these keys: ``traceId``, ``traceParent``,
        ``traceState``, ``baggage``. Only the keys with a non-empty
        value for the current process are included.
    """
    out: dict[str, str] = {}
    xray = os.environ.get("_X_AMZN_TRACE_ID", "").strip()
    if xray:
        out["traceId"] = xray
    return out
