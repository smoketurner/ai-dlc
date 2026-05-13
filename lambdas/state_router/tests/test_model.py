"""Parsing-layer tests for ``state_router.model``.

The parsers consume raw DynamoDB envelope items (``boto3.client``-shaped)
and emit a typed :class:`Run` dataclass. These tests pin the type
contract — in particular the ``Decimal`` → ``int`` and ``set`` → sorted-tuple
coercions that ``TypeDeserializer`` does not perform on its own.

The model no longer carries Task rows; all state lives on the single
``sk=STATE`` row per run.
"""

from __future__ import annotations

from typing import Any

import pytest

from common.state import RunState
from state_router.model import parse_run


def state_row(**overrides: dict[str, Any]) -> dict[str, Any]:
    """Build a raw DDB STATE item with optional field overrides."""
    item: dict[str, Any] = {
        "pk": {"S": "RUN#r-1"},
        "sk": {"S": "STATE"},
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
    assert run.triage_action is None
    assert run.issue_labels == ()
    assert run.pending_revision_feedback == ()
    assert run.dispatch_failure_count == 0
    assert run.revision_count == 0
    assert run.check_state == ""


def test_parse_run_decodes_full_field_set() -> None:
    item = state_row(
        run_id={"S": "r-42"},
        correlation_id={"S": "c-42"},
        project_slug={"S": "demo"},
        intent={"S": "build feature X"},
        requestor={"S": "alice@example.com"},
        actor_id={"S": "alice"},
        current_state={"S": RunState.designed.value},
        triage_action={"S": "proceed"},
        target_repo={"S": "owner/repo"},
        requestor_sub={"S": "sub-1"},
        source_issue_url={"S": "https://github.com/owner/repo/issues/7"},
        source_issue_title={"S": "Add X"},
        source_issue_body={"S": "we need X because Y"},
        issue_number={"N": "7"},
        issue_title={"S": "Add X"},
        issue_body={"S": "..."},
        plan_s3_key={"S": "runs/r-42/plan.md"},
        critique_s3_key={"S": "runs/r-42/critique.md"},
        pr_url={"S": "https://github.com/owner/repo/pull/8"},
        reviewer_verdict={"S": "request_changes"},
        check_state={"S": "failed"},
        revision_count={"N": "2"},
        dispatch_failure_count={"N": "3"},
    )
    run = parse_run(item, [])
    assert run is not None
    assert run.run_id == "r-42"
    assert run.current_state is RunState.designed
    assert run.issue_number == 7
    assert run.dispatch_failure_count == 3
    assert run.target_repo == "owner/repo"
    assert run.plan_s3_key == "runs/r-42/plan.md"
    assert run.critique_s3_key == "runs/r-42/critique.md"
    assert run.reviewer_verdict == "request_changes"
    assert run.check_state == "failed"
    assert run.revision_count == 2
    assert run.source_issue_title == "Add X"
    assert run.source_issue_body == "we need X because Y"


def test_parse_run_sorts_string_sets_for_determinism() -> None:
    item = state_row(
        issue_labels={"SS": ["bug", "area/router", "p1"]},
    )
    run = parse_run(item, [])
    assert run is not None
    assert run.issue_labels == ("area/router", "bug", "p1")


def test_parse_run_uses_run_id_from_pk_when_attribute_missing() -> None:
    item = {"pk": {"S": "RUN#r-from-pk"}, "sk": {"S": "STATE"}}
    run = parse_run(item, [])
    assert run is not None
    assert run.run_id == "r-from-pk"


def test_parse_run_raises_on_unknown_state_value() -> None:
    with pytest.raises(ValueError):
        parse_run(state_row(current_state={"S": "totally-not-a-state"}), [])


def test_parse_run_normalizes_pending_revision_feedback_decimals_to_int() -> None:
    """Pending revision feedback's nested ``Decimal`` fields get coerced to ``int``."""
    item = state_row(
        pending_revision_feedback={
            "L": [
                {
                    "M": {
                        "kind": {"S": "issue_comment_mention"},
                        "comment_id": {"N": "12345"},
                        "body": {"S": "please fix"},
                        "commenter": {"S": "alice"},
                    },
                },
                {
                    "M": {
                        "kind": {"S": "ci_failure"},
                        "workflow_name": {"S": "ci"},
                        "conclusion": {"S": "failure"},
                        "head_sha": {"S": "abc1234"},
                        "html_url": {"S": "https://github.com/o/r/actions/runs/1"},
                    },
                },
            ],
        },
    )
    run = parse_run(item, [])
    assert run is not None
    first, second = run.pending_revision_feedback
    assert first["kind"] == "issue_comment_mention"
    assert first["comment_id"] == 12345
    assert type(first["comment_id"]) is int
    assert second["kind"] == "ci_failure"
    assert second["workflow_name"] == "ci"


def test_parse_run_ignores_task_items() -> None:
    """Lingering TASK rows are simply skipped — there is no Task model any more."""
    task_items = [{"pk": {"S": "RUN#r-1"}, "sk": {"S": "TASK#T-001"}}]
    run = parse_run(state_row(), task_items)
    assert run is not None
    # Run has no `tasks` attribute — the model only carries the STATE row.
    assert not hasattr(run, "tasks")
