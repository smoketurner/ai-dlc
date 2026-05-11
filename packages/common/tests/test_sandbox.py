"""Tests for ``common.sandbox``.

The Lambda + CodeInterpreter SDK are mocked end-to-end so the suite can
verify the redaction, lifecycle, and short-circuit behaviour without
touching AWS.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from common import sandbox as sandbox_mod
from common.agentcore_code_interpreter import CommandResult
from common.errors import AgentCoreCodeInterpreterError
from common.sandbox import (
    EXTRACT_SCRIPT_TEMPLATE,
    SANDBOX_BOOTSTRAP_RELPATH,
    execute_in_sandbox,
    get_pr_diff,
    parse_pr_url,
    redact_archive_token,
    run_pr_in_sandbox,
)


def stream_event(structured: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    """Build the SDK invoke response wrapper for a single result event."""
    return {
        "stream": [
            {
                "result": {
                    "content": [],
                    "structuredContent": structured,
                    "isError": is_error,
                },
            },
        ],
    }


def make_command_result(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = 0,
    is_error: bool = False,
    execution_time: float | None = 0.1,
) -> CommandResult:
    return CommandResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        execution_time_seconds=execution_time,
        is_error=is_error,
    )


def test_parse_pr_url_extracts_repo_and_number() -> None:
    assert parse_pr_url("https://github.com/smoketurner/ai-dlc/pull/42") == (
        "smoketurner/ai-dlc",
        42,
    )


def test_parse_pr_url_returns_none_for_unknown_shape() -> None:
    assert parse_pr_url("https://example.com/o/r/pull/1") is None
    assert parse_pr_url("not a url") is None


def test_redact_archive_token_strips_token_query_param() -> None:
    raw = "urlopen failed: https://codeload.github.com/o/r/legacy.tar.gz/abc?token=AABBCC"
    redacted = redact_archive_token(raw)
    assert "AABBCC" not in redacted
    assert "<redacted>" in redacted


def test_redact_archive_token_handles_token_after_other_params() -> None:
    raw = "url=https://codeload.example/x?ref=abc&token=SECRETXYZ&extra=1"
    redacted = redact_archive_token(raw)
    assert "SECRETXYZ" not in redacted
    assert "extra=1" in redacted


def test_redact_archive_token_no_op_when_absent() -> None:
    assert redact_archive_token("plain text") == "plain text"


def test_execute_in_sandbox_extracts_then_runs_commands_and_stops_session() -> None:
    sdk = MagicMock()
    sdk.start.return_value = "sess-1"
    sdk.execute_code.return_value = stream_event({"exitCode": 0})
    sdk.execute_command.return_value = stream_event(
        {"stdout": "5 passed", "exitCode": 0, "executionTime": 1.2},
    )
    result = execute_in_sandbox(
        sdk,
        ci_id="ci-1",
        archive_url="https://codeload.github.com/o/r/legacy.tar.gz/abc?token=tok",
        head_sha="abc123",
        commands=["uv run pytest -q"],
        working_dir="repo",
    )
    assert result["head_sha"] == "abc123"
    assert result["extract"]["exit_code"] == 0
    assert len(result["results"]) == 1
    assert result["results"][0]["exit_code"] == 0
    assert "5 passed" in result["results"][0]["stdout"]
    sdk.stop.assert_called_once_with()


def test_execute_in_sandbox_passes_archive_url_and_working_dir_into_extract_script() -> None:
    sdk = MagicMock()
    sdk.execute_code.return_value = stream_event({"exitCode": 0})
    sdk.execute_command.return_value = stream_event({"exitCode": 0})
    execute_in_sandbox(
        sdk,
        ci_id="ci-1",
        archive_url="https://codeload.example/o/r.tar.gz?token=t",
        head_sha="abc",
        commands=["true"],
        working_dir="checkout",
    )
    sdk.execute_code.assert_called_once()
    code_arg = sdk.execute_code.call_args.kwargs.get("code") or sdk.execute_code.call_args.args[0]
    assert "checkout" in code_arg
    assert "https://codeload.example/o/r.tar.gz?token=t" in code_arg


def test_execute_in_sandbox_stops_on_extract_failure_and_redacts_token() -> None:
    sdk = MagicMock()
    sdk.execute_code.return_value = stream_event(
        {
            "stdout": "",
            "stderr": (
                "urllib.error.HTTPError: 404 Not Found "
                "https://codeload.github.com/o/r/legacy.tar.gz/abc?token=SECRETTOKEN"
            ),
            "exitCode": 1,
        },
        is_error=True,
    )
    result = execute_in_sandbox(
        sdk,
        ci_id="ci-1",
        archive_url="https://codeload.github.com/o/r/legacy.tar.gz/abc?token=SECRETTOKEN",
        head_sha="abc",
        commands=["uv run pytest"],
        working_dir="repo",
    )
    assert result["extract"]["is_error"] is True
    assert "SECRETTOKEN" not in json.dumps(result)
    assert result["results"] == []
    sdk.execute_command.assert_not_called()
    sdk.stop.assert_called_once_with()


def test_execute_in_sandbox_stops_session_even_when_extract_raises() -> None:
    """A botocore-level failure inside execute_code must not skip stop()."""
    sdk = MagicMock()
    sdk.execute_code.side_effect = ClientError({"Error": {"Code": "X"}}, "Invoke")
    result = execute_in_sandbox(
        sdk,
        ci_id="ci-1",
        archive_url="https://codeload.example/x?token=t",
        head_sha="abc",
        commands=["true"],
        working_dir="repo",
    )
    assert "error" in result
    sdk.stop.assert_called_once_with()


def test_execute_in_sandbox_stops_first_failing_command() -> None:
    sdk = MagicMock()
    sdk.execute_code.return_value = stream_event({"exitCode": 0})
    sdk.execute_command.side_effect = [
        stream_event({"exitCode": 0}),  # cmd 1 ok
        stream_event({"exitCode": 1}, is_error=True),  # cmd 2 fails
    ]
    result = execute_in_sandbox(
        sdk,
        ci_id="ci-1",
        archive_url="https://codeload.example/x?token=t",
        head_sha="abc",
        commands=["echo 1", "echo 2", "echo 3"],
        working_dir="repo",
    )
    # Should run cmd1 + cmd2, not cmd3 (because cmd2 failed)
    assert [r["command"] for r in result["results"]] == ["echo 1", "echo 2"]


def test_run_pr_in_sandbox_returns_error_for_unparseable_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIDLC_CODE_INTERPRETER_ID", "ci-1")
    out = run_pr_in_sandbox("not a url", commands=["true"])
    assert out["error"].startswith("could not parse pr_url")


def test_run_pr_in_sandbox_returns_error_when_ci_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIDLC_CODE_INTERPRETER_ID", raising=False)
    out = run_pr_in_sandbox(
        "https://github.com/o/r/pull/1",
        commands=["true"],
    )
    assert out["error"] == "AIDLC_CODE_INTERPRETER_ID is not set"


def test_run_pr_in_sandbox_full_path_with_mocked_lambda_and_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the full Lambda → SDK round trip with everything stubbed."""
    monkeypatch.setenv("AIDLC_CODE_INTERPRETER_ID", "ci-1")
    monkeypatch.setenv("AIDLC_REPO_HELPER_FUNCTION_NAME", "ai-dlc-dev-repo_helper")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    fake_lambda = MagicMock()
    fake_payload = MagicMock()
    fake_payload.read.return_value = json.dumps(
        {
            "ok": True,
            "op": "get_pr_archive_url",
            "result": {
                "archive_url": "https://codeload.github.com/o/r/legacy.tar.gz/abc?token=t",
                "head_sha": "deadbeef",
            },
        }
    ).encode()
    fake_lambda.invoke.return_value = {"Payload": fake_payload}

    fake_sdk = MagicMock()
    fake_sdk.execute_code.return_value = stream_event({"exitCode": 0})
    fake_sdk.execute_command.return_value = stream_event(
        {"stdout": "ok\n", "exitCode": 0},
    )

    monkeypatch.setattr(sandbox_mod, "lambda_client", lambda: fake_lambda)

    def fake_ci_factory(*, region: str) -> Any:
        del region
        return fake_sdk

    monkeypatch.setattr(sandbox_mod, "CodeInterpreter", fake_ci_factory)

    out = run_pr_in_sandbox(
        "https://github.com/o/r/pull/7",
        commands=["uv run pytest -q"],
    )
    assert out["head_sha"] == "deadbeef"
    assert out["results"][0]["stdout"] == "ok\n"
    fake_lambda.invoke.assert_called_once()
    invoke_kwargs = fake_lambda.invoke.call_args.kwargs
    payload = json.loads(invoke_kwargs["Payload"])
    assert payload["input"]["op"] == "get_pr_archive_url"
    assert payload["input"]["repo"] == "o/r"
    assert payload["input"]["pr_number"] == 7


def test_get_pr_diff_invokes_repo_helper_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent-facing helper rounds-trips through repo_helper.get_pr_diff."""
    monkeypatch.setenv("AIDLC_REPO_HELPER_FUNCTION_NAME", "ai-dlc-dev-repo_helper")

    fake_lambda = MagicMock()
    fake_payload = MagicMock()
    diff_result = {
        "head_sha": "abc",
        "files_truncated": False,
        "files": [
            {
                "filename": "src/foo.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "patch": "@@ +1 @@\n+x",
                "truncated": False,
                "previous_filename": None,
            }
        ],
    }
    fake_payload.read.return_value = json.dumps(
        {"ok": True, "op": "get_pr_diff", "result": diff_result}
    ).encode()
    fake_lambda.invoke.return_value = {"Payload": fake_payload}
    monkeypatch.setattr(sandbox_mod, "lambda_client", lambda: fake_lambda)

    out = get_pr_diff("https://github.com/o/r/pull/7")
    assert out == diff_result
    invoke_payload = json.loads(fake_lambda.invoke.call_args.kwargs["Payload"])
    assert invoke_payload["input"] == {"op": "get_pr_diff", "repo": "o/r", "pr_number": 7}


def test_get_pr_diff_returns_error_for_unparseable_url() -> None:
    out = get_pr_diff("not a url")
    assert out["error"].startswith("could not parse pr_url")


def test_get_pr_diff_returns_error_when_repo_helper_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIDLC_REPO_HELPER_FUNCTION_NAME", raising=False)
    out = get_pr_diff("https://github.com/o/r/pull/7")
    assert "AIDLC_REPO_HELPER_FUNCTION_NAME" in out["error"]


def test_extract_script_template_compiles_with_repr_safe_substitutions() -> None:
    """The template uses ``!r`` so URLs containing quotes can't break the script."""
    rendered = EXTRACT_SCRIPT_TEMPLATE.format(
        archive_url="https://codeload.example/x?token=t'\"hostile",
        working_dir="repo",
        bootstrap_relpath=SANDBOX_BOOTSTRAP_RELPATH,
    )
    compile(rendered, "<test>", "exec")


def test_extract_script_template_runs_per_project_bootstrap() -> None:
    """The script looks for ``.aidlc/sandbox-bootstrap.sh`` and runs it via bash."""
    rendered = EXTRACT_SCRIPT_TEMPLATE.format(
        archive_url="https://codeload.example/x?token=t",
        working_dir="repo",
        bootstrap_relpath=SANDBOX_BOOTSTRAP_RELPATH,
    )
    assert "'.aidlc/sandbox-bootstrap.sh'" in rendered
    assert '["bash", bootstrap]' in rendered
    assert 'raise SystemExit(f"bootstrap exited with' in rendered


def test_command_result_unused_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: unused helper imports stay wired so refactors keep them in scope."""
    del monkeypatch
    assert make_command_result(stdout="x").stdout == "x"


def test_agentcore_error_wraps_intent() -> None:
    """The AgentCoreCodeInterpreterError type is the contract sandbox raises into."""
    err = AgentCoreCodeInterpreterError("test", code_interpreter_id="ci-1")
    assert err.context["code_interpreter_id"] == "ci-1"
