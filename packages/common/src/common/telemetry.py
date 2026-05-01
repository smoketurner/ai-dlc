"""OpenTelemetry + structlog setup for ai-dlc components.

AgentCore Runtime auto-instruments containerized agents when launched via
``opentelemetry-instrument`` (from ``aws-opentelemetry-distro``). Lambdas and
the dashboard need to call :func:`init_telemetry` themselves at cold-start /
process-start; the function is idempotent so calling it twice is harmless.

Span attribute conventions (used by every agent + tool span):

* ``agent.name`` (e.g., ``"architect"``)
* ``agent.framework`` (``"strands"`` | ``"claude-agent-sdk"``)
* ``agent.session_id``
* ``agent.actor_id``
* ``agent.project_slug``
* ``agent.model_id``
* ``agent.token.input`` / ``agent.token.output`` / ``agent.cost_usd``
* ``tool.name`` / ``tool.kind`` / ``tool.duration_ms`` / ``tool.error``
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.trace import Span, Tracer

from common.settings import Settings

_state: dict[str, bool] = {"initialized": False}


def init_telemetry(settings: Settings, /) -> None:
    """Configure structlog and ensure OTEL is wired.

    Idempotent. Safe to call multiple times in Lambda warm-restart cycles.
    """
    if _state["initialized"]:
        return
    _configure_structlog(settings)
    # OTEL provider/exporter wiring is owned by ``aws-opentelemetry-distro``
    # via the ``opentelemetry-instrument`` entrypoint — we don't replicate it
    # here. We simply read tracer instances from the global provider.
    _state["initialized"] = True


def _configure_structlog(settings: Settings, /) -> None:
    """Set up structlog for JSON output + standard library bridging."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "ai-dlc") -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger.

    Args:
        name: Logger name; defaults to ``"ai-dlc"``.
    """
    return structlog.get_logger(name)


def get_tracer(name: str) -> Tracer:
    """Return an OTEL tracer for a module or component."""
    return trace.get_tracer(name)


@contextmanager
def agent_span(
    name: str,
    /,
    *,
    agent_name: str,
    session_id: str,
    project_slug: str,
    framework: str,
    model_id: str,
) -> Iterator[Span]:
    """Open a span for an agent invocation with the canonical attribute set."""
    tracer = get_tracer("ai-dlc.agent")
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("agent.name", agent_name)
        span.set_attribute("agent.framework", framework)
        span.set_attribute("agent.session_id", session_id)
        span.set_attribute("agent.project_slug", project_slug)
        span.set_attribute("agent.model_id", model_id)
        yield span


@contextmanager
def tool_span(name: str, /, *, kind: str) -> Iterator[Span]:
    """Open a span for a tool call.

    Args:
        name: Tool name (e.g., ``"read_repo_structure"``).
        kind: One of ``"mcp"``, ``"sdk_mcp"``, ``"builtin"``.
    """
    tracer = get_tracer("ai-dlc.tool")
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("tool.name", name)
        span.set_attribute("tool.kind", kind)
        yield span


def record_tokens(
    span: Span,
    /,
    *,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    """Record token-usage attributes on the current agent span."""
    span.set_attribute("agent.token.input", input_tokens)
    span.set_attribute("agent.token.output", output_tokens)
    span.set_attribute("agent.cost_usd", cost_usd)


def add_context(**kwargs: Any) -> None:
    """Bind structlog contextvars (sets keys on every subsequent log line)."""
    structlog.contextvars.bind_contextvars(**kwargs)
