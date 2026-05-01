"""Typed exceptions used throughout the ai-dlc platform.

All exceptions inherit from :class:`AidlcError` so callers can catch the entire
domain in one ``except`` clause when they need to. Each subclass carries its own
context dictionary so error messages stay actionable without sprinkling
``f"... {x} {y} {z}"`` strings everywhere.
"""

from __future__ import annotations

from typing import Any


class AidlcError(Exception):
    """Base class for every ai-dlc domain error.

    Attributes:
        message: Human-readable summary.
        context: Structured context for logs and tracing.
    """

    def __init__(self, message: str, /, **context: Any) -> None:
        """Initialize the error with a message and optional structured context."""
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context)

    def __str__(self) -> str:
        """Render ``message (key=value, ...)`` for logs."""
        if not self.context:
            return self.message
        rendered = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
        return f"{self.message} ({rendered})"


class ConfigurationError(AidlcError):
    """A required setting, env var, or SSM parameter is missing or invalid."""


class ValidationError(AidlcError):
    """An input does not match the expected schema or invariant."""


class MemoryDocParseError(ValidationError):
    """A ``MEMORY.md`` file is structurally invalid (e.g., unknown headers)."""


class IdempotencyConflictError(AidlcError):
    """A duplicate request was rejected by the idempotency check."""


class ApprovalConflictError(AidlcError):
    """A HITL approval decision violates separation-of-duties or has expired."""


class CostLimitExceededError(AidlcError):
    """The agent run exceeded its per-run cost cap."""


class GatewayError(AidlcError):
    """An MCP call to AgentCore Gateway failed."""


class AgentCoreMemoryError(AidlcError):
    """An AgentCore Memory operation failed."""


class S3ArtifactError(AidlcError):
    """An S3 artifact read or write failed."""


class GitOpError(AidlcError):
    """A git or GitHub operation failed."""
