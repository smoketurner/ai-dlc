"""Tests for the state_router Lambda handler's batch-failure return shape.

Under the event-driven beacon model, every successfully-processed
beacon is acked to SQS (no batch-item failures). The router's job is
to dispatch any pending action and return; the next state-advancing
event will cause the projector to emit a fresh beacon. Only an
unhandled exception in :func:`process_record` keeps a beacon visible —
SQS retries those under its standard error semantics.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext

from common.state import RunState
from state_router.handler import handler
from state_router.model import Run


def ctx() -> LambdaContext:
    """Minimal LambdaContext stub for powertools."""
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="state_router-test",
            memory_limit_in_mb=512,
            invoked_function_arn="arn:aws:lambda:us-east-1:0:function:state_router",
            aws_request_id="rid-1",
            get_remaining_time_in_millis=lambda: 30_000,
        ),
    )


def beacon(run_id: str, *, message_id: str = "msg-1") -> dict[str, str]:
    """Build an SQS-event record with a ``{"run_id": ...}`` body."""
    return {"messageId": message_id, "body": json.dumps({"run_id": run_id})}


def make_run(state: RunState | None) -> Run:
    return Run(
        run_id="r-1",
        correlation_id="c-1",
        project_slug="demo",
        intent="x",
        requestor="alice",
        actor_id="alice",
        current_state=state,
    )


def test_active_beacon_acked_after_dispatch() -> None:
    """Active runs dispatch and ack — no batch-item failure, beacon deleted."""
    event = {"Records": [beacon("r-1", message_id="msg-active")]}
    with (
        patch("state_router.handler.read_run", return_value=make_run(RunState.tasks_in_progress)),
        patch("state_router.handler.execute") as mock_execute,
    ):
        out = handler(event, ctx())
    assert out == {"batchItemFailures": []}
    assert mock_execute.call_count == 1


def test_terminal_beacon_acked() -> None:
    """A run in a terminal state acks normally; ``decide`` returns Noop."""
    event = {"Records": [beacon("r-1", message_id="msg-done")]}
    with (
        patch("state_router.handler.read_run", return_value=make_run(RunState.done)),
        patch("state_router.handler.execute"),
    ):
        out = handler(event, ctx())
    assert out == {"batchItemFailures": []}


def test_orphan_beacon_returned_as_success() -> None:
    """A beacon for a run that doesn't exist is dropped (delete on success)."""
    event = {"Records": [beacon("r-missing", message_id="msg-orphan")]}
    with patch("state_router.handler.read_run", return_value=None):
        out = handler(event, ctx())
    assert out == {"batchItemFailures": []}


def test_malformed_beacon_returned_as_success() -> None:
    """A beacon with a non-JSON body is dropped (delete on success)."""
    event = {"Records": [{"messageId": "msg-bad", "body": "not-json{"}]}
    out = handler(event, ctx())
    assert out == {"batchItemFailures": []}


def test_beacon_missing_run_id_returned_as_success() -> None:
    """A well-formed body without ``run_id`` is dropped."""
    event = {"Records": [{"messageId": "msg-no-run", "body": "{}"}]}
    out = handler(event, ctx())
    assert out == {"batchItemFailures": []}


def test_mixed_batch_all_acked() -> None:
    """Mixed batch: every beacon is acked under the event-driven model."""
    event = {
        "Records": [
            beacon("r-active", message_id="msg-1"),
            beacon("r-done", message_id="msg-2"),
        ],
    }

    def read(run_id: str) -> Run | None:
        if run_id == "r-active":
            return make_run(RunState.tasks_in_progress)
        if run_id == "r-done":
            return make_run(RunState.done)
        return None

    with (
        patch("state_router.handler.read_run", side_effect=read),
        patch("state_router.handler.execute"),
    ):
        out = handler(event, ctx())
    assert out == {"batchItemFailures": []}


def test_unhandled_exception_propagates() -> None:
    """Unhandled errors propagate so SQS keeps the beacon visible for retry.

    Under the event-driven model, redelivery is no longer a state-machine
    tick — it's pure error retry. The handler does not swallow exceptions
    from ``decide`` / ``execute`` / ``read_run``.
    """
    event = {"Records": [beacon("r-broken", message_id="msg-1")]}
    with (
        patch("state_router.handler.read_run", side_effect=RuntimeError("ddb down")),
        pytest.raises(RuntimeError, match="ddb down"),
    ):
        handler(event, ctx())
