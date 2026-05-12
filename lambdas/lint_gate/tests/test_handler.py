"""Unit tests for lint_gate.handler.

All AWS calls and Code Interpreter SDK are mocked — no real AWS needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext

from lint_gate import handler as handler_mod
from lint_gate.handler import (
    LINT_COMMANDS,
    LintGateInput,
    _build_envelope,
    _infra_failure,
    _run_gate,
    handler,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal env vars required by the handler and powertools."""
    monkeypatch.setenv("AIDLC_BUS_NAME", "aidlc-dev")
    monkeypatch.setenv("AIDLC_CODE_INTERPRETER_ID", "ci-abc123")
    monkeypatch.setenv("AIDLC_REPO_HELPER_FUNCTION_NAME", "repo-helper-dev")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("POWERTOOLS_METRICS_NAMESPACE", "ai-dlc")
    # Clear @cache on sandbox helpers so monkeypatched env is picked up.
    handler_mod.code_interpreter_id.cache_clear()  # ty: ignore[attr-defined]


def ctx() -> LambdaContext:
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="lint-gate-test",
            memory_limit_in_mb=256,
            invoked_function_arn=("arn:aws:lambda:us-east-1:000000000000:function:lint-gate-test"),
            aws_request_id="req-1",
        ),
    )


def base_event(**overrides: Any) -> dict[str, Any]:
    return {
        "project_slug": "ai-dlc",
        "spec_slug": "lint-gate",
        "pr_url": "https://github.com/smoketurner/ai-dlc/pull/99",
        "run_id": "019e0e69-198d-7263-8bfc-7ea2d077b3a6",
        "correlation_id": "019e0e69-198d-7263-8bfc-7eb9e8ae05df",
        "actor_id": "lint_gate",
        **overrides,
    }


def _cmd_entry(cmd: str, exit_code: int = 0, stderr: str = "") -> dict[str, Any]:
    return {
        "command": cmd,
        "stdout": "",
        "stderr": stderr,
        "exit_code": exit_code,
        "is_error": exit_code != 0,
        "execution_time_seconds": 0.1,
    }


def _sandbox_ok(commands: list[str] | None = None) -> dict[str, Any]:
    cmds = commands if commands is not None else list(LINT_COMMANDS)
    return {
        "head_sha": "abc123",
        "extract": {"exit_code": 0, "stderr": "", "is_error": False},
        "results": [_cmd_entry(c) for c in cmds],
        "session_id": "sess-1",
    }


def _sandbox_fail(
    failing_cmd: str,
    stderr: str = "E501 line too long",
) -> dict[str, Any]:
    """Sandbox result where ``failing_cmd`` returns non-zero; rest not run."""
    results = []
    for cmd in LINT_COMMANDS:
        if cmd == failing_cmd:
            results.append(_cmd_entry(cmd, exit_code=1, stderr=stderr))
            break
        results.append(_cmd_entry(cmd))
    return {
        "head_sha": "abc123",
        "extract": {"exit_code": 0, "stderr": "", "is_error": False},
        "results": results,
        "session_id": "sess-1",
    }


def _fake_repo_helper(**_: Any) -> dict[str, Any]:
    return {"archive_url": "u", "head_sha": "abc"}


# ---------------------------------------------------------------------------
# _infra_failure helper
# ---------------------------------------------------------------------------


def test_infra_failure_sets_error_class() -> None:
    r = _infra_failure("something broke")
    assert r["ok"] is False
    assert r["error_class"] == "infrastructure"
    assert r["stderr"] == "something broke"
    assert r["head_sha"] == ""


# ---------------------------------------------------------------------------
# _build_envelope
# ---------------------------------------------------------------------------


def test_build_envelope_passed() -> None:
    inp = LintGateInput.model_validate(base_event())
    result = {
        "ok": True,
        "head_sha": "sha1",
        "session_id": "s1",
        "commands_run": list(LINT_COMMANDS),
    }
    env = _build_envelope(inp, result, 500)
    assert env.type == "LINT_GATE.PASSED"
    assert env.payload.head_sha == "sha1"  # ty: ignore[union-attr]
    assert env.payload.duration_ms == 500  # ty: ignore[union-attr]
    assert list(env.payload.commands_run) == list(LINT_COMMANDS)  # ty: ignore[union-attr]


def test_build_envelope_failed() -> None:
    inp = LintGateInput.model_validate(base_event())
    result = {
        "ok": False,
        "head_sha": "sha2",
        "session_id": "s2",
        "failed_command": "ruff check .",
        "stderr": "E501 line too long",
        "error_class": "lint",
    }
    env = _build_envelope(inp, result, 300)
    assert env.type == "LINT_GATE.FAILED"
    assert env.payload.error_class == "lint"  # ty: ignore[union-attr]
    assert env.payload.failed_command == "ruff check ."  # ty: ignore[union-attr]
    assert env.payload.stderr == "E501 line too long"  # ty: ignore[union-attr]


# ---------------------------------------------------------------------------
# _run_gate — mocked sandbox
# ---------------------------------------------------------------------------


def test_run_gate_passes_all_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_mod, "invoke_repo_helper", _fake_repo_helper)
    with (
        patch.object(handler_mod, "execute_in_sandbox", return_value=_sandbox_ok()),
        patch.object(handler_mod, "CodeInterpreter", return_value=MagicMock()),
    ):
        result = _run_gate(LintGateInput.model_validate(base_event()))
    assert result["ok"] is True
    assert result["head_sha"] == "abc123"
    assert list(result["commands_run"]) == list(LINT_COMMANDS)


def test_run_gate_fails_on_ruff_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_mod, "invoke_repo_helper", _fake_repo_helper)
    sandbox = _sandbox_fail("ruff check .", stderr="bad code")
    with (
        patch.object(handler_mod, "execute_in_sandbox", return_value=sandbox),
        patch.object(handler_mod, "CodeInterpreter", return_value=MagicMock()),
    ):
        result = _run_gate(LintGateInput.model_validate(base_event()))
    assert result["ok"] is False
    assert result["error_class"] == "lint"
    assert result["failed_command"] == "ruff check ."
    assert result["stderr"] == "bad code"


def test_run_gate_fails_on_ruff_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_mod, "invoke_repo_helper", _fake_repo_helper)
    sandbox = _sandbox_fail("ruff format --check .", stderr="reformatted x.py")
    with (
        patch.object(handler_mod, "execute_in_sandbox", return_value=sandbox),
        patch.object(handler_mod, "CodeInterpreter", return_value=MagicMock()),
    ):
        result = _run_gate(LintGateInput.model_validate(base_event()))
    assert result["ok"] is False
    assert result["error_class"] == "format"
    assert result["failed_command"] == "ruff format --check ."


def test_run_gate_fails_on_ty_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_mod, "invoke_repo_helper", _fake_repo_helper)
    sandbox = _sandbox_fail("ty check", stderr="error: type mismatch")
    with (
        patch.object(handler_mod, "execute_in_sandbox", return_value=sandbox),
        patch.object(handler_mod, "CodeInterpreter", return_value=MagicMock()),
    ):
        result = _run_gate(LintGateInput.model_validate(base_event()))
    assert result["ok"] is False
    assert result["error_class"] == "typecheck"
    assert result["failed_command"] == "ty check"


def test_run_gate_stops_on_first_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ruff check fails, format and ty are not run."""
    monkeypatch.setattr(handler_mod, "invoke_repo_helper", _fake_repo_helper)
    sandbox = _sandbox_fail("ruff check .")
    with (
        patch.object(handler_mod, "execute_in_sandbox", return_value=sandbox) as mock_exec,
        patch.object(handler_mod, "CodeInterpreter", return_value=MagicMock()),
    ):
        result = _run_gate(LintGateInput.model_validate(base_event()))
    assert result["ok"] is False
    assert result["error_class"] == "lint"
    # execute_in_sandbox is called once; stop-on-first is inside sandbox.py's
    # run_command_sequence, which the sandbox fixture already models.
    assert mock_exec.call_count == 1


def test_run_gate_infrastructure_failure_session_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """When execute_in_sandbox raises, we get an infrastructure error."""
    monkeypatch.setattr(handler_mod, "invoke_repo_helper", _fake_repo_helper)
    with (
        patch.object(handler_mod, "execute_in_sandbox", side_effect=RuntimeError("session died")),
        patch.object(handler_mod, "CodeInterpreter", return_value=MagicMock()),
    ):
        result = _run_gate(LintGateInput.model_validate(base_event()))
    assert result["ok"] is False
    assert result["error_class"] == "infrastructure"
    assert "session died" in result["stderr"]


def test_run_gate_infrastructure_failure_repo_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """When repo_helper raises, we get an infrastructure error (no sandbox started)."""
    monkeypatch.setattr(
        handler_mod,
        "invoke_repo_helper",
        MagicMock(side_effect=RuntimeError("repo_helper down")),
    )
    result = _run_gate(LintGateInput.model_validate(base_event()))
    assert result["ok"] is False
    assert result["error_class"] == "infrastructure"
    assert "repo_helper down" in result["stderr"]


def test_run_gate_infrastructure_failure_sandbox_error_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandbox returns an 'error' key with no results → infrastructure failure."""
    monkeypatch.setattr(handler_mod, "invoke_repo_helper", _fake_repo_helper)
    bad_sandbox = {
        "head_sha": "sha",
        "extract": None,
        "results": [],
        "error": "extract failed",
    }
    with (
        patch.object(handler_mod, "execute_in_sandbox", return_value=bad_sandbox),
        patch.object(handler_mod, "CodeInterpreter", return_value=MagicMock()),
    ):
        result = _run_gate(LintGateInput.model_validate(base_event()))
    assert result["ok"] is False
    assert result["error_class"] == "infrastructure"


# ---------------------------------------------------------------------------
# handler (Lambda entry point) — event routing + publish
# ---------------------------------------------------------------------------


def _collect_published() -> list[Any]:
    return []


def test_handler_emits_passed_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_mod, "invoke_repo_helper", _fake_repo_helper)
    published: list[Any] = []
    monkeypatch.setattr(handler_mod, "publish", published.append)
    with (
        patch.object(handler_mod, "execute_in_sandbox", return_value=_sandbox_ok()),
        patch.object(handler_mod, "CodeInterpreter", return_value=MagicMock()),
    ):
        out = handler(base_event(), ctx())
    assert out == {"ok": True}
    assert len(published) == 1
    env = published[0]
    assert env.type == "LINT_GATE.PASSED"
    assert env.payload.spec_slug == "lint-gate"


def test_handler_emits_failed_event_on_lint_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_mod, "invoke_repo_helper", _fake_repo_helper)
    published: list[Any] = []
    monkeypatch.setattr(handler_mod, "publish", published.append)
    with (
        patch.object(
            handler_mod,
            "execute_in_sandbox",
            return_value=_sandbox_fail("ruff check ."),
        ),
        patch.object(handler_mod, "CodeInterpreter", return_value=MagicMock()),
    ):
        out = handler(base_event(), ctx())
    assert out == {"ok": False}
    env = published[0]
    assert env.type == "LINT_GATE.FAILED"
    assert env.payload.error_class == "lint"


def test_handler_emits_failed_event_on_infra_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handler_mod,
        "invoke_repo_helper",
        MagicMock(side_effect=RuntimeError("infra gone")),
    )
    published: list[Any] = []
    monkeypatch.setattr(handler_mod, "publish", published.append)
    out = handler(base_event(), ctx())
    assert out == {"ok": False}
    env = published[0]
    assert env.type == "LINT_GATE.FAILED"
    assert env.payload.error_class == "infrastructure"


def test_handler_returns_validation_error_for_bad_input() -> None:
    out = handler({"bad": "input"}, ctx())
    assert out == {"ok": False, "error": "validation_error"}
