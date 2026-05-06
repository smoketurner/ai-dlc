"""Tests for ``common.agentcore_code_interpreter``.

The official SDK class is mocked: the wrapper takes a ``CodeInterpreter``
positionally, so we hand it a ``unittest.mock.MagicMock`` whose method
returns are scripted per-test. botocore errors are simulated by setting
``side_effect`` on the mocked methods.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from common.agentcore_code_interpreter import (
    CodeResult,
    CommandResult,
    execute_code,
    execute_command,
    start_session,
    stop_session,
    upload_file,
)
from common.errors import AgentCoreCodeInterpreterError


def make_command_response(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = 0,
    execution_time: float | None = 0.1,
    is_error: bool = False,
) -> dict[str, object]:
    """Build the SDK's execute_command response shape."""
    structured: dict[str, object] = {}
    if stdout != "":
        structured["stdout"] = stdout
    if stderr != "":
        structured["stderr"] = stderr
    if exit_code is not None:
        structured["exitCode"] = exit_code
    if execution_time is not None:
        structured["executionTime"] = execution_time
    return {
        "sessionId": "sess-1",
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


def test_start_session_returns_session_id() -> None:
    client = MagicMock()
    client.start.return_value = "sess-abc"
    result = start_session(client, code_interpreter_id="ci-1", session_timeout_seconds=300)
    assert result == "sess-abc"
    client.start.assert_called_once_with(
        identifier="ci-1",
        name=None,
        session_timeout_seconds=300,
    )


def test_start_session_wraps_client_errors() -> None:
    client = MagicMock()
    client.start.side_effect = ClientError({"Error": {"Code": "Throttle"}}, "Start")
    with pytest.raises(AgentCoreCodeInterpreterError) as exc:
        start_session(client, code_interpreter_id="ci-1")
    assert exc.value.context["code_interpreter_id"] == "ci-1"


def test_stop_session_calls_stop() -> None:
    client = MagicMock()
    stop_session(client)
    client.stop.assert_called_once_with()


def test_stop_session_wraps_errors() -> None:
    client = MagicMock()
    client.stop.side_effect = ClientError({"Error": {"Code": "X"}}, "Stop")
    with pytest.raises(AgentCoreCodeInterpreterError):
        stop_session(client)


def test_execute_command_parses_structured_content() -> None:
    client = MagicMock()
    client.execute_command.return_value = make_command_response(
        stdout="hello\n",
        stderr="",
        exit_code=0,
        execution_time=0.42,
    )
    result = execute_command(client, command="echo hello")
    assert isinstance(result, CommandResult)
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.exit_code == 0
    assert result.execution_time_seconds == pytest.approx(0.42)
    assert result.is_error is False


def test_execute_command_surfaces_nonzero_exit_and_error_flag() -> None:
    client = MagicMock()
    client.execute_command.return_value = make_command_response(
        stdout="",
        stderr="boom",
        exit_code=2,
        is_error=True,
    )
    result = execute_command(client, command="false")
    assert result.exit_code == 2
    assert result.is_error is True
    assert result.stderr == "boom"


def test_execute_command_wraps_boto_errors_without_leaking_command() -> None:
    """The error context MUST NOT include the command — install tokens may be in it."""
    client = MagicMock()
    client.execute_command.side_effect = ClientError({"Error": {"Code": "X"}}, "Invoke")
    secret_command = "git clone https://x-access-token:ghs_supersecret@github.com/o/r.git"  # noqa: S105
    with pytest.raises(AgentCoreCodeInterpreterError) as exc:
        execute_command(client, command=secret_command)
    assert "ghs_supersecret" not in str(exc.value)
    assert "ghs_supersecret" not in repr(exc.value.context)


def test_execute_command_raises_on_sandbox_exception_event() -> None:
    """Sandbox-level exception events become AgentCoreCodeInterpreterError."""
    client = MagicMock()
    client.execute_command.return_value = {
        "stream": [
            {"throttlingException": {"message": "slow down"}},
        ],
    }
    with pytest.raises(AgentCoreCodeInterpreterError) as exc:
        execute_command(client, command="x")
    assert exc.value.context["exception_kind"] == "throttlingException"


def test_execute_command_raises_when_no_result_event() -> None:
    client = MagicMock()
    client.execute_command.return_value = {"stream": []}
    with pytest.raises(AgentCoreCodeInterpreterError):
        execute_command(client, command="x")


def test_execute_code_returns_text_payload() -> None:
    client = MagicMock()
    client.execute_code.return_value = {
        "stream": [
            {
                "result": {
                    "content": [
                        {"type": "text", "text": "42\n"},
                        {"type": "text", "text": "done\n"},
                    ],
                    "structuredContent": {
                        "stdout": "42\n",
                        "stderr": "",
                        "exitCode": 0,
                        "executionTime": 0.05,
                    },
                    "isError": False,
                },
            },
        ],
    }
    result = execute_code(client, code="print(40+2)")
    assert isinstance(result, CodeResult)
    assert result.text == "42\ndone\n"
    assert result.stdout == "42\n"
    assert result.exit_code == 0


def test_execute_code_wraps_value_error() -> None:
    """SDK's language validation raises ValueError — wrapper surfaces it."""
    client = MagicMock()
    client.execute_code.side_effect = ValueError("bad language")
    with pytest.raises(AgentCoreCodeInterpreterError):
        execute_code(client, code="x", language="ruby")


def test_upload_file_passes_through_args() -> None:
    client = MagicMock()
    upload_file(client, path="data.csv", content="a,b\n1,2\n", description="sample")
    client.upload_file.assert_called_once_with(
        path="data.csv",
        content="a,b\n1,2\n",
        description="sample",
    )


def test_upload_file_wraps_value_error() -> None:
    """SDK rejects absolute paths with ValueError — wrapper converts it."""
    client = MagicMock()
    client.upload_file.side_effect = ValueError("absolute path")
    with pytest.raises(AgentCoreCodeInterpreterError) as exc:
        upload_file(client, path="/absolute", content="x")
    assert exc.value.context["path"] == "/absolute"
