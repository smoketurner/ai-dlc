"""Shared Code Interpreter sandbox helpers used by Tester + Reviewer.

Both agents need the same flow: parse a GitHub PR URL, mint an
authenticated clone URL via ``repo_helper.mint_clone_token``, start a CI
session, clone + checkout the PR head, run a series of shell commands,
and clean up. This module owns that flow so each agent's ``tools.py``
just wraps :func:`run_pr_in_sandbox` with Strands' ``@tool``.

The clone URL embeds a short-lived GitHub installation token. To avoid
leaking it into CloudWatch:
  * we never include the command in error context (see
    :mod:`common.agentcore_code_interpreter`),
  * we redact the token portion of any sandbox stderr we surface, and
  * we never log the token directly here.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import structlog
from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

from common import agentcore_code_interpreter as ci
from common.errors import AgentCoreCodeInterpreterError

if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient

logger = structlog.get_logger()

PR_URL_PATTERN = re.compile(r"^https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<num>\d+)$")
TOKEN_REDACT_PATTERN = re.compile(r"x-access-token:[^@\s]+@")
SANDBOX_OUTPUT_TAIL_BYTES = 4096
SANDBOX_CLONE_STDERR_TAIL_BYTES = 2048


@cache
def lambda_client() -> LambdaClient:
    """Process-cached boto3 Lambda client (for invoking ``repo_helper``)."""
    return boto3.client("lambda")


def repo_helper_function_name() -> str | None:
    """Lambda function name of the ``repo_helper`` tool — None when unset."""
    return os.environ.get("AIDLC_REPO_HELPER_FUNCTION_NAME") or None


def code_interpreter_id() -> str | None:
    """AgentCore Code Interpreter resource id — None when unset."""
    return os.environ.get("AIDLC_CODE_INTERPRETER_ID") or None


def aws_region() -> str:
    """AWS region the agent runtime is deployed in."""
    return os.environ["AWS_REGION"]


def parse_pr_url(pr_url: str) -> tuple[str, int] | None:
    """Pull ``(owner/repo, pr_number)`` out of a github.com PR URL."""
    match = PR_URL_PATTERN.match(pr_url)
    if match is None:
        return None
    return match.group("repo"), int(match.group("num"))


def redact_clone_token(text: str) -> str:
    """Redact any ``x-access-token:<token>@`` occurrence from log-bound text."""
    return TOKEN_REDACT_PATTERN.sub("x-access-token:<redacted>@", text)


def invoke_repo_helper(*, op: str, **fields: Any) -> dict[str, Any]:
    """Invoke ``repo_helper`` with one op and return the ``result`` payload.

    Raises:
        RuntimeError: When ``AIDLC_REPO_HELPER_FUNCTION_NAME`` is unset, the
            Lambda invocation fails, or the response carries an error
            envelope.
    """
    fn = repo_helper_function_name()
    if fn is None:
        msg = "AIDLC_REPO_HELPER_FUNCTION_NAME is not set"
        raise RuntimeError(msg)
    response = lambda_client().invoke(
        FunctionName=fn,
        InvocationType="RequestResponse",
        Payload=json.dumps({"input": {"op": op, **fields}}).encode("utf-8"),
    )
    body = json.loads(response["Payload"].read())
    if not body.get("ok"):
        kind = body.get("error", {}).get("kind", "unknown")
        msg = f"repo_helper.{op} returned error envelope: {kind}"
        raise RuntimeError(msg)
    result = body.get("result")
    if not isinstance(result, dict):
        msg = f"repo_helper.{op} response had no result: {body!r}"
        raise TypeError(msg)
    return result


def run_pr_in_sandbox(
    pr_url: str,
    commands: list[str],
    working_dir: str = "repo",
) -> dict[str, Any]:
    """Clone the PR head into an AgentCore Code Interpreter sandbox and run shell commands.

    Use this to actually execute the diff's tests against a clean checkout.
    The clone uses a short-lived installation token from ``repo_helper``;
    the token is never returned and stderr is redacted before reporting.
    Each command runs from the cloned repo's root, in order, stopping at
    the first non-zero exit.

    Args:
        pr_url: GitHub PR URL — ``https://github.com/owner/name/pull/123``.
        commands: Shell commands to run from the repo root, in order.
        working_dir: Sandbox-relative directory the repo is cloned into.

    Returns:
        ``{"head_sha": str, "clone": {...}, "results": [{...}]}`` on
        success; ``{"error": str}`` on configuration failure.
    """
    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return {"error": f"could not parse pr_url: {pr_url!r}"}
    repo, pr_number = parsed
    ci_id = code_interpreter_id()
    if ci_id is None:
        return {"error": "AIDLC_CODE_INTERPRETER_ID is not set"}
    try:
        token = invoke_repo_helper(op="mint_clone_token", repo=repo, pr_number=pr_number)
    except (RuntimeError, TypeError) as exc:
        return {"error": str(exc)}
    sdk_client = CodeInterpreter(region=aws_region())
    return execute_in_sandbox(
        sdk_client,
        ci_id=ci_id,
        clone_url=str(token["clone_url"]),
        head_sha=str(token["head_sha"]),
        commands=commands,
        working_dir=working_dir,
    )


def execute_in_sandbox(
    sdk_client: CodeInterpreter,
    /,
    *,
    ci_id: str,
    clone_url: str,
    head_sha: str,
    commands: list[str],
    working_dir: str,
) -> dict[str, Any]:
    """Run the clone-and-execute flow on an existing CI SDK client.

    Split out from :func:`run_pr_in_sandbox` so tests can drive it without
    monkeypatching ``boto3.client``. The session is started here and
    always stopped in ``finally``.
    """
    results: list[dict[str, Any]] = []
    clone_summary: dict[str, Any] | None = None
    try:
        ci.start_session(sdk_client, code_interpreter_id=ci_id)
        clone_summary, ok = run_clone_step(
            sdk_client,
            clone_url=clone_url,
            head_sha=head_sha,
            working_dir=working_dir,
        )
        if not ok:
            return {"head_sha": head_sha, "clone": clone_summary, "results": []}
        results = run_command_sequence(
            sdk_client,
            commands=commands,
            working_dir=working_dir,
        )
    except AgentCoreCodeInterpreterError as exc:
        logger.warning("sandbox session failed", err=str(exc))
        return {
            "head_sha": head_sha,
            "clone": clone_summary,
            "results": results,
            "error": str(exc),
        }
    finally:
        try:
            ci.stop_session(sdk_client)
        except AgentCoreCodeInterpreterError as exc:
            logger.warning("stop_session failed", err=str(exc))
    return {"head_sha": head_sha, "clone": clone_summary, "results": results}


def run_clone_step(
    sdk_client: CodeInterpreter,
    /,
    *,
    clone_url: str,
    head_sha: str,
    working_dir: str,
) -> tuple[dict[str, Any], bool]:
    """Execute ``git clone && git checkout`` in the sandbox.

    Returns the redacted summary plus a success flag.
    """
    cmd = (
        f"git clone {shlex.quote(clone_url)} {shlex.quote(working_dir)} && "
        f"git -C {shlex.quote(working_dir)} checkout {shlex.quote(head_sha)}"
    )
    result = ci.execute_command(sdk_client, command=cmd)
    summary = {
        "exit_code": result.exit_code,
        "stderr": redact_clone_token(result.stderr)[-SANDBOX_CLONE_STDERR_TAIL_BYTES:],
        "is_error": result.is_error,
    }
    ok = not result.is_error and (result.exit_code == 0 or result.exit_code is None)
    return summary, ok


def run_command_sequence(
    sdk_client: CodeInterpreter,
    /,
    *,
    commands: list[str],
    working_dir: str,
) -> list[dict[str, Any]]:
    """Run each command from ``working_dir``; stop on the first non-zero exit."""
    out: list[dict[str, Any]] = []
    for cmd in commands:
        wrapped = f"cd {shlex.quote(working_dir)} && {cmd}"
        result = ci.execute_command(sdk_client, command=wrapped)
        out.append(
            {
                "command": cmd,
                "stdout": result.stdout[-SANDBOX_OUTPUT_TAIL_BYTES:],
                "stderr": result.stderr[-SANDBOX_OUTPUT_TAIL_BYTES:],
                "exit_code": result.exit_code,
                "is_error": result.is_error,
                "execution_time_seconds": result.execution_time_seconds,
            }
        )
        if result.exit_code is not None and result.exit_code != 0:
            break
    return out
