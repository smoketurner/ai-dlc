"""Thin wrapper around the AgentCore Code Interpreter SDK.

The official ``bedrock_agentcore.tools.code_interpreter_client.CodeInterpreter``
already exposes ``execute_code`` / ``execute_command`` / ``upload_file`` /
``stop`` and parses the streaming response. This module:

  * narrows the surface to the calls the platform actually uses,
  * converts the streaming response dicts into typed dataclasses, and
  * normalises every botocore / SDK error to
    :class:`AgentCoreCodeInterpreterError` so callers catch a single type.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

from common.errors import AgentCoreCodeInterpreterError

if TYPE_CHECKING:
    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of a shell command in the sandbox.

    Attributes:
        stdout: Captured stdout text (may be empty).
        stderr: Captured stderr text (may be empty).
        exit_code: Process exit code; ``None`` if the SDK didn't surface one
            (e.g., the command was killed by a hard timeout).
        execution_time_seconds: Wall-clock duration the SDK reports.
        is_error: ``True`` when the SDK flagged the result as an error
            (typically a non-zero exit or a sandbox-level failure).
    """

    stdout: str
    stderr: str
    exit_code: int | None
    execution_time_seconds: float | None
    is_error: bool


@dataclass(frozen=True, slots=True)
class CodeResult:
    """Outcome of an ``executeCode`` call.

    Mirrors :class:`CommandResult` plus the rendered text content (the SDK
    returns model-friendly content blocks for code outputs — we collapse
    them to a single string).
    """

    stdout: str
    stderr: str
    exit_code: int | None
    execution_time_seconds: float | None
    is_error: bool
    text: str


def start_session(
    client: CodeInterpreter,
    /,
    *,
    code_interpreter_id: str,
    name: str | None = None,
    session_timeout_seconds: int = 600,
) -> str:
    """Start a sandbox session on ``client``. Returns the session id.

    Args:
        client: A constructed ``CodeInterpreter`` SDK instance (caller picks
            region + boto3 session).
        code_interpreter_id: Resource id of the AgentCore Code Interpreter
            (the ``AIDLC_CODE_INTERPRETER_ID`` env var).
        name: Optional session name; the SDK auto-generates one when omitted.
        session_timeout_seconds: Hard idle timeout. Default 600s — high
            enough for most test suites, low enough to release sessions
            quickly on agent crash.
    """
    try:
        return client.start(
            identifier=code_interpreter_id,
            name=name,
            session_timeout_seconds=session_timeout_seconds,
        )
    except (BotoCoreError, ClientError) as exc:
        raise AgentCoreCodeInterpreterError(
            "start_session failed",
            code_interpreter_id=code_interpreter_id,
        ) from exc


def stop_session(client: CodeInterpreter, /) -> None:
    """Stop the active session on ``client``. Idempotent.

    Errors are wrapped so the caller can run this in a ``finally`` block
    without losing the original exception's traceback.
    """
    try:
        client.stop()
    except (BotoCoreError, ClientError) as exc:
        raise AgentCoreCodeInterpreterError("stop_session failed") from exc


def execute_command(client: CodeInterpreter, /, *, command: str) -> CommandResult:
    """Run a shell command in the sandbox and return the parsed result.

    Args:
        client: SDK instance with an active session (call :func:`start_session`
            first).
        command: Shell command (single line; chain with ``&&`` or wrap in
            ``bash -lc '...'`` for multi-step flows).
    """
    try:
        response = client.execute_command(command)
    except (BotoCoreError, ClientError) as exc:
        # Don't preview the command in the error context — callers like the
        # Tester pass commands containing short-lived install tokens
        # (``git clone https://x-access-token:<token>@...``) that must not
        # land in CloudWatch logs.
        raise AgentCoreCodeInterpreterError("execute_command failed") from exc
    structured, content_text, is_error = parse_invoke_response(response)
    return CommandResult(
        stdout=str(structured.get("stdout", "")),
        stderr=str(structured.get("stderr", "")) or content_text,
        exit_code=as_optional_int(structured.get("exitCode")),
        execution_time_seconds=as_optional_float(structured.get("executionTime")),
        is_error=is_error,
    )


def execute_code(
    client: CodeInterpreter,
    /,
    *,
    code: str,
    language: str = "python",
    clear_context: bool = False,
) -> CodeResult:
    """Run code in the sandbox and return the parsed result.

    Args:
        client: SDK instance with an active session.
        code: Source to execute.
        language: ``"python"`` (default), ``"javascript"``, or ``"typescript"``.
        clear_context: Drop interpreter state before running. Python only.
    """
    try:
        response = client.execute_code(code=code, language=language, clear_context=clear_context)
    except (BotoCoreError, ClientError, ValueError) as exc:
        raise AgentCoreCodeInterpreterError(
            "execute_code failed",
            language=language,
        ) from exc
    structured, content_text, is_error = parse_invoke_response(response)
    return CodeResult(
        stdout=str(structured.get("stdout", "")),
        stderr=str(structured.get("stderr", "")),
        exit_code=as_optional_int(structured.get("exitCode")),
        execution_time_seconds=as_optional_float(structured.get("executionTime")),
        is_error=is_error,
        text=content_text,
    )


def upload_file(
    client: CodeInterpreter,
    /,
    *,
    path: str,
    content: str | bytes,
    description: str = "",
) -> None:
    """Write a file into the sandbox at ``path`` (relative).

    Args:
        client: SDK instance with an active session.
        path: Sandbox-relative path (no leading ``/``).
        content: Text or binary blob; binary is base64-encoded by the SDK.
        description: Optional semantic description carried as SDK metadata.
    """
    try:
        client.upload_file(path=path, content=content, description=description)
    except (BotoCoreError, ClientError, ValueError) as exc:
        raise AgentCoreCodeInterpreterError(
            "upload_file failed",
            path=path,
        ) from exc


def parse_invoke_response(response: Mapping[str, Any]) -> tuple[dict[str, Any], str, bool]:
    """Drain the SDK invoke response into (structured, text, is_error).

    The SDK returns ``{"sessionId": ..., "stream": <iterable of events>}``.
    Each event is one of:

      * ``{"result": {"content": [...], "structuredContent": {...},
        "isError": bool}}`` — success / structured failure
      * ``{"<exception>Exception": {"message": ...}}`` — sandbox-level error

    We pick the first ``result`` event we see, flatten the ``content`` list
    into a single string (joining ``text`` fields), and surface the
    ``structuredContent`` dict to the caller. If no ``result`` event
    appears, the first exception event becomes the error.
    """
    stream = response.get("stream", []) if isinstance(response, Mapping) else []
    for event in stream:
        if not isinstance(event, Mapping):
            continue
        result = event.get("result")
        if isinstance(result, Mapping):
            structured = result.get("structuredContent") or {}
            text_parts = [
                str(item.get("text", ""))
                for item in result.get("content", [])
                if isinstance(item, Mapping) and item.get("type") == "text"
            ]
            is_error = bool(result.get("isError"))
            return dict(structured), "".join(text_parts), is_error
        for key, value in event.items():
            if key.endswith("Exception") and isinstance(value, Mapping):
                message = str(value.get("message", "<no message>"))
                raise AgentCoreCodeInterpreterError(
                    "sandbox returned exception event",
                    exception_kind=key,
                    message=message,
                )
    raise AgentCoreCodeInterpreterError("invoke response had no result event")


def as_optional_int(value: Any) -> int | None:
    """Coerce a numeric SDK field to ``int`` if present."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def as_optional_float(value: Any) -> float | None:
    """Coerce a numeric SDK field to ``float`` if present."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None
