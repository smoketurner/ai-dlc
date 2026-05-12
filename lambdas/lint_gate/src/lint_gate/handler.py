"""Lint gate Lambda — deterministic ruff + ty check on the impl branch.

Invoked by the state_router when a run reaches ``tasks_complete``.
Clones the impl branch into an AgentCore Code Interpreter sandbox,
runs ``ruff check .``, ``ruff format --check .``, and ``ty check`` in
order (stopping on first failure), then emits ``LINT_GATE.PASSED`` or
``LINT_GATE.FAILED`` on the platform EventBridge bus.

Input shape (from state_router ``InvokeLambda`` or direct invocation):
  {
    "project_slug": str,
    "spec_slug":    str,
    "pr_url":       str,   # unified impl PR URL
    "run_id":       str,
    "correlation_id": str,
    "actor_id":     str,
  }
"""

from __future__ import annotations

import time
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext
from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.event_emit import publish
from common.events import EventEnvelope, LintGateFailed, LintGatePassed
from common.ids import CorrelationId, RunId
from common.sandbox import (
    aws_region,
    code_interpreter_id,
    execute_in_sandbox,
    invoke_repo_helper,
    parse_pr_url,
)

logger = Logger(service="lint_gate")
tracer = Tracer(service="lint_gate")
metrics = Metrics(namespace="ai-dlc", service="lint_gate")

LINT_COMMANDS: tuple[str, ...] = (
    "ruff check .",
    "ruff format --check .",
    "ty check",
)
_COMMAND_CLASS: dict[str, str] = {
    "ruff check .": "lint",
    "ruff format --check .": "format",
    "ty check": "typecheck",
}
_STDERR_TAIL = 4096


class LintGateInput(BaseModel):
    """Validated input for the lint gate Lambda."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=False)

    project_slug: str = Field(min_length=1, max_length=64)
    spec_slug: str = Field(min_length=1, max_length=128)
    pr_url: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    actor_id: str = Field(default="lint_gate", min_length=1)


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Run the lint gate and emit the outcome event."""
    try:
        inp = LintGateInput.model_validate(event)
    except ValidationError as exc:
        logger.exception("invalid input", extra={"errors": exc.errors()})
        return {"ok": False, "error": "validation_error"}

    start_ms = int(time.monotonic() * 1000)
    result = _run_gate(inp)
    duration_ms = int(time.monotonic() * 1000) - start_ms

    publish(_build_envelope(inp, result, duration_ms))
    metrics.add_metric(
        name="LintGateRun",
        unit=MetricUnit.Count,
        value=1,
    )
    if result.get("ok"):
        metrics.add_metric(name="LintGatePassed", unit=MetricUnit.Count, value=1)
    else:
        metrics.add_metric(name="LintGateFailed", unit=MetricUnit.Count, value=1)
    return {"ok": result.get("ok", False)}


def _fetch_archive(pr_url: str) -> dict[str, Any]:
    """Parse the PR URL, resolve the Code Interpreter id, and fetch the archive.

    Returns a dict with ``"ok": True`` plus ``ci_id``, ``archive_url``, and
    ``head_sha`` on success; ``"ok": False`` plus infra-failure fields on any
    error.
    """
    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return _infra_failure(f"could not parse pr_url: {pr_url!r}")
    ci_id = code_interpreter_id()
    if ci_id is None:
        return _infra_failure("AIDLC_CODE_INTERPRETER_ID is not set")
    repo, pr_number = parsed
    try:
        archive = invoke_repo_helper(op="get_pr_archive_url", repo=repo, pr_number=pr_number)
    except (RuntimeError, TypeError) as exc:
        return _infra_failure(str(exc))
    return {
        "ok": True,
        "ci_id": ci_id,
        "archive_url": str(archive.get("archive_url", "")),
        "head_sha": str(archive.get("head_sha", "")),
    }


def _run_gate(inp: LintGateInput) -> dict[str, Any]:
    """Extract the impl branch into a sandbox and run the lint commands.

    Returns a dict with either:
      {"ok": True, "head_sha": str, "commands_run": list[str], "session_id": str}
    or:
      {"ok": False, "head_sha": str, "session_id": str,
       "failed_command": str, "stderr": str, "error_class": str}
    or (infrastructure failure):
      {"ok": False, "head_sha": "", "session_id": "",
       "failed_command": "", "stderr": str, "error_class": "infrastructure"}
    """
    fetch = _fetch_archive(inp.pr_url)
    if not fetch.get("ok"):
        return fetch

    head_sha: str = fetch["head_sha"]
    sdk_client = CodeInterpreter(region=aws_region())
    try:
        sandbox_result = execute_in_sandbox(
            sdk_client,
            ci_id=str(fetch["ci_id"]),
            archive_url=str(fetch["archive_url"]),
            head_sha=head_sha,
            commands=list(LINT_COMMANDS),
            working_dir="repo",
        )
    except Exception as exc:
        sandbox_result = {"error": str(exc), "results": [], "session_id": ""}

    session_id = str(sandbox_result.get("session_id", ""))
    if "error" in sandbox_result and not sandbox_result.get("results"):
        return _infra_failure(
            str(sandbox_result["error"]),
            head_sha=head_sha,
            session_id=session_id,
        )

    results: list[dict[str, Any]] = list(sandbox_result.get("results", []))
    commands_run: list[str] = []
    for entry in results:
        cmd = str(entry.get("command", ""))
        commands_run.append(cmd)
        exit_code = entry.get("exit_code")
        is_error = bool(entry.get("is_error"))
        if (exit_code is not None and exit_code != 0) or is_error:
            stderr_raw = str(entry.get("stderr", ""))
            stdout_raw = str(entry.get("stdout", ""))
            return {
                "ok": False,
                "head_sha": head_sha,
                "session_id": session_id,
                "failed_command": cmd,
                "stderr": (stderr_raw or stdout_raw)[-_STDERR_TAIL:],
                "error_class": _COMMAND_CLASS.get(cmd, "lint"),
            }

    return {
        "ok": True,
        "head_sha": head_sha,
        "session_id": session_id,
        "commands_run": commands_run,
    }


def _infra_failure(
    reason: str,
    *,
    head_sha: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Build an infrastructure-failure result dict."""
    return {
        "ok": False,
        "head_sha": head_sha,
        "session_id": session_id,
        "failed_command": "",
        "stderr": reason[-_STDERR_TAIL:],
        "error_class": "infrastructure",
    }


def _build_envelope(
    inp: LintGateInput,
    result: dict[str, Any],
    duration_ms: int,
) -> EventEnvelope[LintGatePassed] | EventEnvelope[LintGateFailed]:
    """Build the typed EventEnvelope from the gate result."""
    run_id = RunId(inp.run_id)
    correlation_id = CorrelationId(inp.correlation_id)
    if result.get("ok"):
        return EventEnvelope[LintGatePassed](
            type="LINT_GATE.PASSED",
            run_id=run_id,
            correlation_id=correlation_id,
            actor_id=inp.actor_id,
            payload=LintGatePassed(
                project_slug=inp.project_slug,
                spec_slug=inp.spec_slug,
                pr_url=inp.pr_url,
                head_sha=str(result.get("head_sha", "")),
                commands_run=list(result.get("commands_run", [])),
                duration_ms=duration_ms,
                session_id=str(result.get("session_id", "")),
            ),
        )
    return EventEnvelope[LintGateFailed](
        type="LINT_GATE.FAILED",
        run_id=run_id,
        correlation_id=correlation_id,
        actor_id=inp.actor_id,
        payload=LintGateFailed(
            project_slug=inp.project_slug,
            spec_slug=inp.spec_slug,
            pr_url=inp.pr_url,
            head_sha=str(result.get("head_sha", "")),
            failed_command=str(result.get("failed_command", "")),
            stderr=str(result.get("stderr", ""))[-_STDERR_TAIL:],
            error_class=result.get("error_class", "infrastructure"),
            duration_ms=duration_ms,
            session_id=str(result.get("session_id", "")),
        ),
    )
