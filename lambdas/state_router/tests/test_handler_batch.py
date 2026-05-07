"""Tests for the state_router Lambda handler's batch-failure return shape.

The handler relies on Lambda's ``ReportBatchItemFailures`` event-source
contract: every active beacon is reported as a batch-item failure so
SQS keeps it visible (the visibility timeout is what schedules the
next state-machine tick). Terminal / orphan / malformed beacons are
omitted from the failures list so SQS auto-deletes them on success.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

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


def test_active_beacon_reported_as_failure() -> None:
    """An active (non-terminal) run keeps its beacon visible via batchItemFailures."""
    event = {"Records": [beacon("r-1", message_id="msg-active")]}
    with (
        patch("state_router.handler.read_run", return_value=make_run(RunState.tasks_in_progress)),
        patch("state_router.handler.execute"),
    ):
        out = handler(event, ctx())
    assert out == {"batchItemFailures": [{"itemIdentifier": "msg-active"}]}


def test_terminal_beacon_returned_as_success() -> None:
    """A run in a terminal state is omitted from failures, so SQS deletes it."""
    event = {"Records": [beacon("r-1", message_id="msg-done")]}
    with patch("state_router.handler.read_run", return_value=make_run(RunState.done)):
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


def test_mixed_batch_only_actives_in_failures() -> None:
    """Handler segregates the batch — actives kept, terminals deleted."""
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
    assert out["batchItemFailures"] == [{"itemIdentifier": "msg-1"}]
