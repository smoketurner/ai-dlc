"""Shared Code Interpreter sandbox helpers used by Tester + Reviewer.

Both agents need the same flow: parse a GitHub PR URL, ask
``repo_helper`` for a short-lived signed tarball URL pinned to the PR
head, start a CI session, download + extract the tarball into the
sandbox via Python ``urllib`` + ``tarfile`` (no ``git`` binary required),
run a series of shell commands, and clean up. This module owns that
flow so each agent's ``tools.py`` just wraps :func:`run_pr_in_sandbox`
with Strands' ``@tool``.

The tarball URL is a ``codeload.github.com`` URL whose query string
carries a short-lived signed token. We still redact the ``?token=``
parameter out of any sandbox stderr we surface, and we never log the
URL directly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

from common import agentcore_code_interpreter as ci
from common.errors import AgentCoreCodeInterpreterError

if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient

logger = logging.getLogger(__name__)

PR_URL_PATTERN = re.compile(r"^https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<num>\d+)$")
ARCHIVE_TOKEN_REDACT_PATTERN = re.compile(r"([?&]token=)[^&\s]+")
SANDBOX_OUTPUT_TAIL_BYTES = 4096
SANDBOX_EXTRACT_STDERR_TAIL_BYTES = 2048


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


def redact_archive_token(text: str) -> str:
    """Redact any ``?token=<token>`` query parameter from log-bound text."""
    return ARCHIVE_TOKEN_REDACT_PATTERN.sub(r"\1<redacted>", text)


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
    """Extract the PR head into an AgentCore Code Interpreter sandbox and run shell commands.

    Use this to actually execute the diff's tests against a clean checkout.
    The PR head is downloaded as a GitHub tarball via a short-lived signed
    URL (no ``git`` binary required inside the sandbox); the URL's token
    is redacted before any stderr is reported. Each command runs from the
    extracted repo's root, in order, stopping at the first non-zero exit.

    Args:
        pr_url: GitHub PR URL — ``https://github.com/owner/name/pull/123``.
        commands: Shell commands to run from the repo root, in order.
        working_dir: Sandbox-relative directory the repo is extracted into.

    Returns:
        ``{"head_sha": str, "extract": {...}, "results": [{...}]}`` on
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
        archive = invoke_repo_helper(
            op="get_pr_archive_url",
            repo=repo,
            pr_number=pr_number,
        )
    except (RuntimeError, TypeError) as exc:
        return {"error": str(exc)}
    sdk_client = CodeInterpreter(region=aws_region())
    return execute_in_sandbox(
        sdk_client,
        ci_id=ci_id,
        archive_url=str(archive["archive_url"]),
        head_sha=str(archive["head_sha"]),
        commands=commands,
        working_dir=working_dir,
    )


def execute_in_sandbox(
    sdk_client: CodeInterpreter,
    /,
    *,
    ci_id: str,
    archive_url: str,
    head_sha: str,
    commands: list[str],
    working_dir: str,
) -> dict[str, Any]:
    """Run the extract-and-execute flow on an existing CI SDK client.

    Split out from :func:`run_pr_in_sandbox` so tests can drive it without
    monkeypatching ``boto3.client``. The session is started here and
    always stopped in ``finally``.
    """
    results: list[dict[str, Any]] = []
    extract_summary: dict[str, Any] | None = None
    try:
        ci.start_session(sdk_client, code_interpreter_id=ci_id)
        extract_summary, ok = run_extract_step(
            sdk_client,
            archive_url=archive_url,
            working_dir=working_dir,
        )
        if not ok:
            return {"head_sha": head_sha, "extract": extract_summary, "results": []}
        results = run_command_sequence(
            sdk_client,
            commands=commands,
            working_dir=working_dir,
        )
    except AgentCoreCodeInterpreterError as exc:
        logger.warning("sandbox session failed", extra={"err": str(exc)})
        return {
            "head_sha": head_sha,
            "extract": extract_summary,
            "results": results,
            "error": str(exc),
        }
    finally:
        try:
            ci.stop_session(sdk_client)
        except AgentCoreCodeInterpreterError as exc:
            logger.warning("stop_session failed", extra={"err": str(exc)})
    return {"head_sha": head_sha, "extract": extract_summary, "results": results}


SANDBOX_BOOTSTRAP_RELPATH = ".aidlc/sandbox-bootstrap.sh"

EXTRACT_SCRIPT_TEMPLATE = """\
import io, os, subprocess, sys, tarfile, urllib.request
url = {archive_url!r}
working_dir = {working_dir!r}
bootstrap_relpath = {bootstrap_relpath!r}
os.makedirs(working_dir, exist_ok=True)
with urllib.request.urlopen(url) as resp:
    raw = resp.read()
with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
    members = tf.getmembers()
    if not members:
        raise SystemExit("empty archive")
    prefix = members[0].name.split("/", 1)[0] + "/"
    for m in members:
        if m.name.startswith(prefix):
            m.name = m.name[len(prefix):]
        if m.name:
            tf.extract(m, path=working_dir, filter="data")
# Per-project sandbox bootstrap: each project owns its own setup recipe at
# ``.aidlc/sandbox-bootstrap.sh`` (install package managers, sync deps, etc.).
# Absent → no-op. Non-zero exit → fail the extract step so the agent doesn't
# run commands against a half-set-up workspace.
bootstrap = os.path.join(working_dir, bootstrap_relpath)
if os.path.exists(bootstrap):
    print(f"running {{bootstrap}}", flush=True)
    proc = subprocess.run(
        ["bash", bootstrap],
        cwd=working_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise SystemExit(f"bootstrap exited with {{proc.returncode}}")
else:
    print(f"no {{bootstrap_relpath}} — skipping setup", flush=True)
print("ok")
"""


def run_extract_step(
    sdk_client: CodeInterpreter,
    /,
    *,
    archive_url: str,
    working_dir: str,
) -> tuple[dict[str, Any], bool]:
    """Download + extract the PR tarball into ``working_dir`` inside the sandbox.

    Uses Python's ``urllib`` + ``tarfile`` via ``execute_code`` rather
    than shelling out, since the managed Code Interpreter does not ship
    ``git``. Returns the redacted summary plus a success flag.
    """
    code = EXTRACT_SCRIPT_TEMPLATE.format(
        archive_url=archive_url,
        working_dir=working_dir,
        bootstrap_relpath=SANDBOX_BOOTSTRAP_RELPATH,
    )
    result = ci.execute_code(sdk_client, code=code, language="python")
    summary = {
        "exit_code": result.exit_code,
        "stderr": redact_archive_token(result.stderr)[-SANDBOX_EXTRACT_STDERR_TAIL_BYTES:],
        "is_error": result.is_error,
    }
    ok = not result.is_error and (result.exit_code == 0 or result.exit_code is None)
    return summary, ok


def get_pr_diff(pr_url: str) -> dict[str, Any]:
    """Fetch a PR's per-file diff metadata via ``repo_helper.get_pr_diff``.

    Returns the structured result on success (``{"head_sha", "files",
    "files_truncated"}``) or ``{"error": str}`` on a URL parse or
    Lambda invocation failure.

    Args:
        pr_url: GitHub PR URL — ``https://github.com/owner/name/pull/123``.
    """
    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return {"error": f"could not parse pr_url: {pr_url!r}"}
    repo, pr_number = parsed
    try:
        return invoke_repo_helper(op="get_pr_diff", repo=repo, pr_number=pr_number)
    except (RuntimeError, TypeError) as exc:
        return {"error": str(exc)}


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
