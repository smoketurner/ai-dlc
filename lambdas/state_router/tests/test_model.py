"""Parsing-layer tests for ``state_router.model``.

The parsers consume raw DynamoDB envelope items (``boto3.client``-shaped)
and emit typed dataclasses. These tests pin the type contract — in
particular the ``Decimal`` → ``int`` and ``set`` → sorted-tuple coercions
that ``TypeDeserializer`` does not perform on its own.
"""

from __future__ import annotations

from typing import Any

import pytest

from common.state import RunState, TaskState
from state_router.model import parse_run, parse_task


def state_row(**overrides: dict[str, Any]) -> dict[str, Any]:
    """Build a raw DDB STATE item with optional field overrides."""
    item: dict[str, Any] = {
        "pk": {"S": "RUN#r-1"},
        "sk": {"S": "STATE"},
    }
    item.update(overrides)
    return item


def task_row(task_id: str, status: str, **overrides: dict[str, Any]) -> dict[str, Any]:
    """Build a raw DDB TASK item with optional field overrides."""
    item: dict[str, Any] = {
        "pk": {"S": "RUN#r-1"},
        "sk": {"S": f"TASK#{task_id}"},
        "status": {"S": status},
    }
    item.update(overrides)
    return item


def test_parse_run_returns_none_for_missing_state_row() -> None:
    assert parse_run({}, []) is None


def test_parse_run_applies_defaults_for_minimum_fields() -> None:
    run = parse_run(state_row(), [])
    assert run is not None
    assert run.run_id == "r-1"
    assert run.correlation_id == ""
    assert run.actor_id == "system"
    assert run.current_state is None
    assert run.workflow_kind is None
    assert run.issue_labels == ()
    assert run.task_ids == ()
    assert run.tasks == ()
    assert run.dispatch_failure_count == 0


def test_parse_run_decodes_full_field_set() -> None:
    item = state_row(
        run_id={"S": "r-42"},
        correlation_id={"S": "c-42"},
        project_slug={"S": "demo"},
        intent={"S": "build feature X"},
        requestor={"S": "alice@example.com"},
        actor_id={"S": "alice"},
        current_state={"S": RunState.spec_drafted.value},
        workflow_kind={"S": "spec_driven"},
        triage_action={"S": "spec"},
        target_repo={"S": "owner/repo"},
        requestor_sub={"S": "sub-1"},
        source_issue_url={"S": "https://github.com/owner/repo/issues/7"},
        issue_number={"N": "7"},
        issue_title={"S": "Add X"},
        issue_body={"S": "..."},
        spec_slug={"S": "add-x"},
        spec_s3_prefix={"S": "s3://b/specs/add-x/"},
        pr_url={"S": "https://github.com/owner/repo/pull/8"},
        synthetic_spec_slug={"S": "add-x-synthetic"},
        dispatch_failure_count={"N": "3"},
    )
    run = parse_run(item, [])
    assert run is not None
    assert run.run_id == "r-42"
    assert run.current_state is RunState.spec_drafted
    assert run.issue_number == 7
    assert run.dispatch_failure_count == 3
    assert run.target_repo == "owner/repo"
    assert run.synthetic_spec_slug == "add-x-synthetic"


def test_parse_run_sorts_string_sets_for_determinism() -> None:
    item = state_row(
        issue_labels={"SS": ["bug", "area/router", "p1"]},
        task_ids={"SS": ["T-002", "T-001", "T-003"]},
    )
    run = parse_run(item, [])
    assert run is not None
    assert run.issue_labels == ("area/router", "bug", "p1")
    assert run.task_ids == ("T-001", "T-002", "T-003")


def test_parse_run_uses_run_id_from_pk_when_attribute_missing() -> None:
    item = {"pk": {"S": "RUN#r-from-pk"}, "sk": {"S": "STATE"}}
    run = parse_run(item, [])
    assert run is not None
    assert run.run_id == "r-from-pk"


def test_parse_run_raises_on_unknown_state_value() -> None:
    with pytest.raises(ValueError):
        parse_run(state_row(current_state={"S": "totally-not-a-state"}), [])


def test_parse_run_includes_parsed_tasks() -> None:
    tasks = [
        task_row("T-001", TaskState.pending.value),
        task_row("T-002", TaskState.merged.value),
    ]
    run = parse_run(state_row(), tasks)
    assert run is not None
    assert len(run.tasks) == 2
    assert {t.task_id for t in run.tasks} == {"T-001", "T-002"}


def test_parse_task_decodes_scalars_and_sets() -> None:
    item = task_row(
        "T-001",
        TaskState.pr_open.value,
        pr_url={"S": "https://github.com/owner/repo/pull/9"},
        pr_number={"N": "9"},
        iteration_count={"N": "2"},
        delivery_ids={"SS": ["d-2", "d-1"]},
        dispatch_failure_count={"N": "1"},
    )
    task = parse_task(item)
    assert task.task_id == "T-001"
    assert task.state is TaskState.pr_open
    assert task.pr_number == 9
    assert type(task.pr_number) is int
    assert task.iteration_count == 2
    assert type(task.iteration_count) is int
    assert task.delivery_ids == frozenset({"d-1", "d-2"})
    assert task.dispatch_failure_count == 1


def test_parse_task_defaults_when_optional_fields_missing() -> None:
    task = parse_task(task_row("T-x", TaskState.pending.value))
    assert task.pr_url is None
    assert task.pr_number is None
    assert task.iteration_count == 0
    assert task.delivery_ids == frozenset()
    assert task.pending_feedback == ()
    assert task.dispatch_failure_count == 0


def test_parse_task_normalizes_feedback_decimals_to_int() -> None:
    item = task_row(
        "T-001",
        TaskState.iterating.value,
        pending_feedback={
            "L": [
                {
                    "M": {
                        "comment_id": {"N": "12345"},
                        "body": {"S": "please fix"},
                        "author": {"S": "bob"},
                    },
                },
                {
                    "M": {
                        "comment_id": {"N": "67890"},
                        "body": {"S": "and this too"},
                        "author": {"S": "carol"},
                    },
                },
            ],
        },
    )
    task = parse_task(item)
    assert len(task.pending_feedback) == 2
    first, second = task.pending_feedback
    assert first["comment_id"] == 12345
    assert type(first["comment_id"]) is int
    assert first["body"] == "please fix"
    assert second["comment_id"] == 67890
    assert type(second["comment_id"]) is int


def test_parse_task_defaults_status_when_missing() -> None:
    item = {"pk": {"S": "RUN#r-1"}, "sk": {"S": "TASK#T-x"}}
    task = parse_task(item)
    assert task.state is TaskState.pending
