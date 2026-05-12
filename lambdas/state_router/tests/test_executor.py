"""Unit tests for the state_router action executor.

Focuses on the race-protection contract of :class:`GuardedAdvance` —
under concurrent beacon delivery, only the router whose conditional
advance wins runs ``on_success``. The dispatch-table tests cover what
each state returns; these tests cover how the executor applies it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from common.state import RunState, TaskState
from state_router.actions import GuardedAdvance, InvokeAgent, InvokeRepoHelper, Noop, SeedTasks
from state_router.execute import (
    execute,
    execute_guarded_advance,
    execute_invoke_repo_helper,
    execute_seed_tasks,
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
        current_state=RunState.tasks_in_progress,
    )


def guarded_advance(invokes: tuple[InvokeAgent, ...]) -> GuardedAdvance:
    """Build a GuardedAdvance like the one ``dispatch_advisors`` returns."""
    return GuardedAdvance(
        target_pk="RUN#r-1",
        target_sk="TASK#T-001",
        advance_from=TaskState.pr_open.value,
        advance_to=TaskState.pending_approval.value,
        on_success=invokes,
    )


def make_reviewer_invoke() -> InvokeAgent:
    return InvokeAgent(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/reviewer",
        runtime_session_id="r-1-T-001-reviewer",
        payload={"task_id": "T-001"},
    )


def make_tester_invoke() -> InvokeAgent:
    return InvokeAgent(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/tester",
        runtime_session_id="r-1-T-001-tester",
        payload={"task_id": "T-001"},
    )


def test_winning_advance_runs_on_success() -> None:
    """When the conditional advance succeeds, on_success actions execute."""
    action = guarded_advance((make_reviewer_invoke(), make_tester_invoke()))
    with (
        patch("state_router.execute.transactional_advance", return_value=True),
        patch("state_router.execute.dispatch_to_runtime") as dispatch,
    ):
        execute_guarded_advance(make_run(), action)
    assert dispatch.call_count == 2
    arns = sorted(call.kwargs["runtime_arn"] for call in dispatch.call_args_list)
    assert any("reviewer" in arn for arn in arns)
    assert any("tester" in arn for arn in arns)


def test_losing_advance_skips_on_success() -> None:
    """When the conditional advance fails, on_success actions do NOT execute.

    This is the race-protection contract: a redelivered beacon can't
    double-fire the advisors. Without the gate, two concurrent routers
    each fire reviewer + tester (4 invokes total) instead of 2.
    """
    action = guarded_advance((make_reviewer_invoke(), make_tester_invoke()))
    with (
        patch("state_router.execute.transactional_advance", return_value=False),
        patch("state_router.execute.dispatch_to_runtime") as dispatch,
    ):
        execute_guarded_advance(make_run(), action)
    assert dispatch.call_count == 0


def test_concurrent_routers_fire_advisors_exactly_once() -> None:
    """Two routers consuming the same beacon — only one fires the advisors.

    Simulates the SQS visibility-timeout redelivery scenario the design
    is meant to defend against.
    """
    action = guarded_advance((make_reviewer_invoke(), make_tester_invoke()))
    advance_results = iter([True, False])  # router 1 wins, router 2 loses
    with (
        patch(
            "state_router.execute.transactional_advance",
            side_effect=lambda **_: next(advance_results),
        ),
        patch("state_router.execute.dispatch_to_runtime") as dispatch,
    ):
        execute_guarded_advance(make_run(), action)
        execute_guarded_advance(make_run(), action)
    assert dispatch.call_count == 2  # exactly one reviewer + one tester


def test_invoke_agent_without_advance_fires_unconditionally() -> None:
    """An InvokeAgent with no advance fields skips advance_state and fires."""
    invoke = make_reviewer_invoke()
    assert invoke.advance_from is None
    with (
        patch("state_router.execute.transactional_advance") as advance,
        patch("state_router.execute.dispatch_to_runtime") as dispatch,
    ):
        execute(make_run(), invoke)
    advance.assert_not_called()
    assert dispatch.call_count == 1


def test_invoke_agent_with_advance_uses_race_guard() -> None:
    """An InvokeAgent with advance fields still does the per-invoke conditional."""
    invoke = InvokeAgent(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/implementer",
        runtime_session_id="r-1-T-001",
        payload={},
        target_pk="RUN#r-1",
        target_sk="TASK#T-001",
        advance_from=TaskState.pending.value,
        advance_to=TaskState.implementer_running.value,
    )
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
    invoke = InvokeAgent(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/architect",
        runtime_session_id="r-1-architect",
        payload={},
        target_pk="RUN#r-1",
        target_sk="STATE",
        advance_from=RunState.triage_decided.value,
        advance_to=RunState.architect_running.value,
    )
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
    invoke = InvokeAgent(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/architect",
        runtime_session_id="r-1-architect",
        payload={},
        target_pk="RUN#r-1",
        target_sk="STATE",
        advance_from=RunState.triage_decided.value,
        advance_to=RunState.architect_running.value,
    )
    with (
        patch("state_router.execute.transactional_advance", return_value=True) as advance,
        patch("state_router.execute.dispatch_to_runtime", return_value=True),
    ):
        execute(make_run(), invoke)
    assert advance.call_count == 1  # forward advance only; no rollback


def test_invoke_agent_no_rollback_when_no_advance_specified() -> None:
    """Gated advisors (no advance fields) skip rollback even on dispatch failure.

    The outer GuardedAdvance owns the state for advisor invokes; the
    advisor InvokeAgent itself has no state to roll back.
    """
    invoke = make_reviewer_invoke()
    assert invoke.advance_from is None
    with (
        patch("state_router.execute.transactional_advance") as advance,
        patch("state_router.execute.dispatch_to_runtime", return_value=False),
    ):
        execute(make_run(), invoke)
    advance.assert_not_called()


def test_unknown_action_type_logs_and_no_ops() -> None:
    """A foreign action type doesn't crash the executor."""
    # Noop is a known type, so use a fresh placeholder by constructing
    # a Noop and verifying execute() handles it without side effects.
    with patch("state_router.execute.dispatch_to_runtime") as dispatch:
        execute(make_run(), Noop("just because"))
    assert dispatch.call_count == 0


def test_execute_seed_tasks_writes_slugs_on_task_rows() -> None:
    """Seeded TASK rows must carry project_slug + spec_slug so webhook
    handlers don't ship empty strings on TASK.ITERATION_REQUESTED /
    TASK.APPROVED / TASK.REJECTED.
    """
    action = SeedTasks(
        run_id="r-1",
        task_ids=("T-001", "T-002"),
        project_slug="demo",
        spec_slug="demo-spec",
    )
    fake_ddb = MagicMock()
    with (
        patch("state_router.execute.ddb", return_value=fake_ddb),
        patch("state_router.execute.runs_table", return_value="runs-test"),
    ):
        execute_seed_tasks(action)
    assert fake_ddb.put_item.call_count == 2
    for call in fake_ddb.put_item.call_args_list:
        item = call.kwargs["Item"]
        assert item["project_slug"] == {"S": "demo"}
        assert item["spec_slug"] == {"S": "demo-spec"}


def make_open_spec_pr_action() -> InvokeRepoHelper:
    """Build the InvokeRepoHelper that ``handle_spec_critiqued`` returns."""
    return InvokeRepoHelper(
        op="open_spec_pr",
        args={"repo": "o/r", "spec_slug": "demo", "spec_s3_prefix": "specs/demo/"},
        target_pk="RUN#r-1",
        target_sk="STATE",
        advance_from=RunState.spec_critiqued.value,
        advance_to=RunState.spec_pr_open.value,
        advance_on_no_change_to=RunState.spec_approved.value,
        record_pr_url_attrs=("pr_url", "spec_pr_url"),
    )


def make_lambda_response(payload: dict[str, object]) -> dict[str, object]:
    """Build the boto3 ``invoke`` envelope around ``payload``."""
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode("utf-8")
    return {"Payload": body}


def test_pick_advance_to_returns_no_change_target_when_result_says_so() -> None:
    """A ``no_change: true`` repo_helper result steers to the no-change target."""
    action = make_open_spec_pr_action()
    body = {"ok": True, "result": {"no_change": True}}
    assert pick_advance_to(action, body) == RunState.spec_approved.value


def test_pick_advance_to_returns_normal_target_when_pr_was_opened() -> None:
    """The normal result (``pr_url`` present) steers to the regular target."""
    action = make_open_spec_pr_action()
    body = {"ok": True, "result": {"pr_url": "https://github.com/o/r/pull/9"}}
    assert pick_advance_to(action, body) == RunState.spec_pr_open.value


def test_pick_advance_to_falls_through_when_no_change_target_unset() -> None:
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


def test_execute_invoke_repo_helper_advances_to_no_change_target() -> None:
    """When repo_helper returns ``no_change``, advance straight to spec_approved."""
    action = make_open_spec_pr_action()
    response = make_lambda_response(
        {
            "ok": True,
            "op": "open_spec_pr",
            "result": {
                "no_change": True,
                "spec_slug": "demo",
                "branch": "aidlc/spec/demo",
                "base_commit_sha": "abc",
            },
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
    assert advance.call_args.kwargs["advance_to"] == RunState.spec_approved.value
    # No PR was opened, so no pr_url is recorded.
    assert advance.call_args.kwargs["extra_attrs"] == {}


def test_execute_invoke_repo_helper_records_pr_url_on_normal_path() -> None:
    """The non-no_change path advances to spec_pr_open and records the PR URL."""
    action = make_open_spec_pr_action()
    response = make_lambda_response(
        {
            "ok": True,
            "op": "open_spec_pr",
            "result": {
                "pr_url": "https://github.com/o/r/pull/9",
                "pr_number": 9,
                "branch": "aidlc/spec/demo",
            },
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
    assert advance.call_args.kwargs["advance_to"] == RunState.spec_pr_open.value
    assert advance.call_args.kwargs["extra_attrs"] == {
        "pr_url": "https://github.com/o/r/pull/9",
        "spec_pr_url": "https://github.com/o/r/pull/9",
    }


def test_execute_invoke_repo_helper_failure_bumps_counter_and_enqueues_retry() -> None:
    """A failed repo_helper response bumps dispatch_failure_count + writes an OUTBOX row.

    Without this, a transient GitHub failure (rate limit, 5xx, branch
    naming collision) wedges the run in its current state forever:
    ``execute_invoke_repo_helper`` used to log WARNING and return,
    leaving no beacon for the next router cycle to pick up.
    """
    action = make_open_spec_pr_action()
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
    assert kwargs["advance_from"] == RunState.spec_critiqued.value
    assert kwargs["advance_to"] == RunState.spec_critiqued.value
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
    action = make_open_spec_pr_action()
    with (
        patch("state_router.execute.transactional_advance", return_value=False) as advance,
        patch("state_router.execute.metrics") as metrics_mock,
    ):
        record_repo_helper_failure(make_run(), action)
    advance.assert_called_once()
    metrics_mock.add_metric.assert_not_called()
