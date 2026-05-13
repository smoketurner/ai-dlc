"""Unit tests for the state_router action executor.

The dispatch-table tests cover what each state returns; these tests
cover how the executor applies it. The router now consists of just a
handful of action types — the GuardedAdvance / DedupedAdvisors /
OpenImplPr / SeedTasks / WriteSyntheticSpec actions that supported
the spec/task fan-out are gone.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from common.state import RunState
from state_router.actions import InvokeAgent, InvokeRepoHelper, Noop
from state_router.execute import (
    execute,
    execute_invoke_repo_helper,
    pick_advance_to,
    record_repo_helper_failure,
)
from state_router.model import Run


def make_run() -> Run:
    """Minimal Run fixture for executor tests."""
    return Run(
        run_id="r-1",
        correlation_id="c-1",
        project_slug="demo",
        intent="x",
        requestor="alice",
        actor_id="alice",
        current_state=RunState.implementer_running,
    )


def reviewer_invoke() -> InvokeAgent:
    """Validator invoke shape: no advance fields (parallel under a single AdvanceState)."""
    return InvokeAgent(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/reviewer",
        runtime_session_id="r-1-reviewer-r0",
        runtime_user_id="gh:tester",
        payload={"pr_url": "https://github.com/o/r/pull/1"},
    )


def architect_invoke() -> InvokeAgent:
    """Run-level invoke with the conditional state advance as race guard."""
    return InvokeAgent(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/architect",
        runtime_session_id="r-1-architect",
        runtime_user_id="gh:tester",
        payload={},
        target_pk="RUN#r-1",
        target_sk="STATE",
        advance_from=RunState.triage_decided.value,
        advance_to=RunState.architect_running.value,
    )


def open_pr_action() -> InvokeRepoHelper:
    """A repo_helper action with an advance — same shape the triage_ask path uses."""
    return InvokeRepoHelper(
        op="comment_pr",
        args={"repo": "o/r", "pr_number": 1, "body": "..."},
        target_pk="RUN#r-1",
        target_sk="STATE",
        advance_from=RunState.validation_complete.value,
        advance_to=RunState.awaiting_human_merge.value,
        record_pr_url_attrs=("pr_url",),
    )


def make_lambda_response(payload: dict[str, object]) -> dict[str, object]:
    """Build the boto3 ``invoke`` envelope around ``payload``."""
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode("utf-8")
    return {"Payload": body}


def test_invoke_agent_without_advance_fires_unconditionally() -> None:
    """An InvokeAgent with no advance fields skips advance_state and fires."""
    invoke = reviewer_invoke()
    assert invoke.advance_from is None
    with (
        patch("state_router.execute.transactional_advance") as advance,
        patch("state_router.execute.dispatch_to_runtime") as dispatch,
    ):
        execute(make_run(), invoke)
    advance.assert_not_called()
    assert dispatch.call_count == 1


def test_invoke_agent_with_advance_uses_race_guard() -> None:
    """An InvokeAgent with advance fields does the per-invoke conditional."""
    invoke = architect_invoke()
    with (
        patch("state_router.execute.transactional_advance", return_value=False) as advance,
        patch("state_router.execute.dispatch_to_runtime") as dispatch,
    ):
        execute(make_run(), invoke)
    advance.assert_called_once()
    assert dispatch.call_count == 0  # lost race; no dispatch


def test_invoke_agent_rolls_back_on_dispatch_failure() -> None:
    """Synchronous dispatch failure (4xx/5xx) reverts the state advance.

    Without rollback, a misconfigured agent or transient runtime error
    leaves the run wedged in ``*_running`` forever — no completion event
    can arrive because the agent never ran. Reverting lets the next
    beacon cycle re-dispatch from the original state.
    """
    invoke = architect_invoke()
    advance_results = iter([True, True])  # forward, then rollback
    with (
        patch(
            "state_router.execute.transactional_advance",
            side_effect=lambda **_: next(advance_results),
        ) as advance,
        patch("state_router.execute.dispatch_to_runtime", return_value=False),
    ):
        execute(make_run(), invoke)
    assert advance.call_count == 2
    forward, rollback = advance.call_args_list
    assert forward.kwargs["advance_from"] == RunState.triage_decided.value
    assert forward.kwargs["advance_to"] == RunState.architect_running.value
    # Rollback shape: reversed direction + counter bump.
    assert rollback.kwargs["advance_from"] == RunState.architect_running.value
    assert rollback.kwargs["advance_to"] == RunState.triage_decided.value
    assert rollback.kwargs["extra_increments"] == {"dispatch_failure_count": 1}


def test_invoke_agent_no_rollback_on_dispatch_success() -> None:
    """A successful dispatch leaves the state at ``*_running``."""
    invoke = architect_invoke()
    with (
        patch("state_router.execute.transactional_advance", return_value=True) as advance,
        patch("state_router.execute.dispatch_to_runtime", return_value=True),
    ):
        execute(make_run(), invoke)
    assert advance.call_count == 1  # forward advance only; no rollback


def test_invoke_agent_no_rollback_when_no_advance_specified() -> None:
    """Validator invokes (no advance fields) skip rollback even on dispatch failure.

    The outer AdvanceState in the compound owns the state for validator
    invokes; the validator InvokeAgent itself has no state to roll back.
    """
    invoke = reviewer_invoke()
    assert invoke.advance_from is None
    with (
        patch("state_router.execute.transactional_advance") as advance,
        patch("state_router.execute.dispatch_to_runtime", return_value=False),
    ):
        execute(make_run(), invoke)
    advance.assert_not_called()


def test_unknown_action_type_logs_and_no_ops() -> None:
    """A foreign action type doesn't crash the executor."""
    with patch("state_router.execute.dispatch_to_runtime") as dispatch:
        execute(make_run(), Noop("just because"))
    assert dispatch.call_count == 0


def test_pick_advance_to_returns_no_change_target_when_result_says_so() -> None:
    """A ``no_change: true`` repo_helper result steers to the no-change target."""
    action = InvokeRepoHelper(
        op="comment_pr",
        args={},
        target_pk="RUN#r-1",
        target_sk="STATE",
        advance_from="x",
        advance_to="y",
        advance_on_no_change_to="z",
    )
    body = {"ok": True, "result": {"no_change": True}}
    assert pick_advance_to(action, body) == "z"


def test_pick_advance_to_returns_normal_target_when_no_change_unset() -> None:
    """An action without ``advance_on_no_change_to`` ignores the flag."""
    action = InvokeRepoHelper(
        op="comment_pr",
        args={},
        target_pk="RUN#r-1",
        target_sk="STATE",
        advance_from="x",
        advance_to="y",
    )
    body = {"ok": True, "result": {"no_change": True}}
    assert pick_advance_to(action, body) == "y"


def test_execute_invoke_repo_helper_advances_on_success() -> None:
    """A successful repo_helper call advances state per the action's fields."""
    action = open_pr_action()
    response = make_lambda_response(
        {
            "ok": True,
            "op": "comment_pr",
            "result": {"pr_url": "https://github.com/o/r/pull/9"},
        },
    )
    fake_lambda = MagicMock()
    fake_lambda.invoke.return_value = response
    with (
        patch("state_router.execute.lambda_client", return_value=fake_lambda),
        patch("state_router.execute.repo_helper_function_name", return_value="repo-helper-fn"),
        patch("state_router.execute.transactional_advance", return_value=True) as advance,
    ):
        execute_invoke_repo_helper(make_run(), action)
    advance.assert_called_once()
    assert advance.call_args.kwargs["advance_to"] == RunState.awaiting_human_merge.value
    assert advance.call_args.kwargs["extra_attrs"] == {
        "pr_url": "https://github.com/o/r/pull/9",
    }


def test_execute_invoke_repo_helper_failure_bumps_counter_and_enqueues_retry() -> None:
    """A failed repo_helper response bumps dispatch_failure_count + writes an OUTBOX row.

    Without this, a transient GitHub failure (rate limit, 5xx) wedges
    the run in its current state forever: the executor used to log
    WARNING and return, leaving no beacon for the next router cycle to
    pick up.
    """
    action = open_pr_action()
    response = make_lambda_response(
        {
            "ok": False,
            "error": {"kind": "github_http_error", "detail": {"status_code": 422}},
        },
    )
    fake_lambda = MagicMock()
    fake_lambda.invoke.return_value = response
    with (
        patch("state_router.execute.lambda_client", return_value=fake_lambda),
        patch("state_router.execute.repo_helper_function_name", return_value="repo-helper-fn"),
        patch("state_router.execute.transactional_advance", return_value=True) as advance,
    ):
        execute_invoke_repo_helper(make_run(), action)
    advance.assert_called_once()
    # No-op SET on state (advance_from == advance_to) — the call is the
    # race guard + counter bump + OUTBOX put, not a state move.
    kwargs = advance.call_args.kwargs
    assert kwargs["advance_from"] == RunState.validation_complete.value
    assert kwargs["advance_to"] == RunState.validation_complete.value
    assert kwargs["extra_increments"] == {"dispatch_failure_count": 1}
    assert "last_dispatch_failure_at" in kwargs["extra_attrs"]


def test_execute_invoke_repo_helper_failure_skipped_when_no_advance_fields() -> None:
    """Informational ops (``comment_issue`` / ``label_issue``) skip the retry path.

    They have no ``advance_from`` to use as a race guard, and they're
    chained with a follow-up action (e.g., ``RUN.CANCEL_REQUESTED``)
    that runs regardless of whether the GitHub call succeeded.
    """
    action = InvokeRepoHelper(
        op="label_issue",
        args={"repo": "o/r", "issue_number": 1, "labels": ["aidlc:declined"]},
    )
    response = make_lambda_response({"ok": False, "error": {"kind": "github_http_error"}})
    fake_lambda = MagicMock()
    fake_lambda.invoke.return_value = response
    with (
        patch("state_router.execute.lambda_client", return_value=fake_lambda),
        patch("state_router.execute.repo_helper_function_name", return_value="repo-helper-fn"),
        patch("state_router.execute.transactional_advance") as advance,
    ):
        execute_invoke_repo_helper(make_run(), action)
    advance.assert_not_called()


def test_record_repo_helper_failure_no_op_when_state_already_moved() -> None:
    """If the projector already moved the row forward, the bump conditional fails.

    ``transactional_advance`` returning ``False`` means the conditional
    update lost the race — the metric is not emitted because no counter
    actually bumped.
    """
    action = open_pr_action()
    with (
        patch("state_router.execute.transactional_advance", return_value=False) as advance,
        patch("state_router.execute.metrics") as metrics_mock,
    ):
        record_repo_helper_failure(make_run(), action)
    advance.assert_called_once()
    metrics_mock.add_metric.assert_not_called()
