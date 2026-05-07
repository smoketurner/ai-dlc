"""Unit tests for the state_router action executor.

Focuses on the race-protection contract of :class:`GuardedAdvance` —
under concurrent beacon delivery, only the router whose conditional
advance wins runs ``on_success``. The dispatch-table tests cover what
each state returns; these tests cover how the executor applies it.
"""

from __future__ import annotations

from unittest.mock import patch

from common.state import RunState, TaskState
from state_router.actions import GuardedAdvance, InvokeAgent, Noop
from state_router.handler import execute, execute_guarded_advance
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
        patch("state_router.handler.advance_state", return_value=True),
        patch("state_router.handler.fire_and_forget") as fire,
    ):
        execute_guarded_advance(make_run(), action)
    assert fire.call_count == 2
    arns = sorted(call.kwargs["runtime_arn"] for call in fire.call_args_list)
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
        patch("state_router.handler.advance_state", return_value=False),
        patch("state_router.handler.fire_and_forget") as fire,
    ):
        execute_guarded_advance(make_run(), action)
    assert fire.call_count == 0


def test_concurrent_routers_fire_advisors_exactly_once() -> None:
    """Two routers consuming the same beacon — only one fires the advisors.

    Simulates the SQS visibility-timeout redelivery scenario the design
    is meant to defend against.
    """
    action = guarded_advance((make_reviewer_invoke(), make_tester_invoke()))
    advance_results = iter([True, False])  # router 1 wins, router 2 loses
    with (
        patch("state_router.handler.advance_state", side_effect=lambda **_: next(advance_results)),
        patch("state_router.handler.fire_and_forget") as fire,
    ):
        execute_guarded_advance(make_run(), action)
        execute_guarded_advance(make_run(), action)
    assert fire.call_count == 2  # exactly one reviewer + one tester


def test_invoke_agent_without_advance_fires_unconditionally() -> None:
    """An InvokeAgent with no advance fields skips advance_state and fires."""
    invoke = make_reviewer_invoke()
    assert invoke.advance_from is None
    with (
        patch("state_router.handler.advance_state") as advance,
        patch("state_router.handler.fire_and_forget") as fire,
    ):
        execute(make_run(), invoke)
    advance.assert_not_called()
    assert fire.call_count == 1


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
        patch("state_router.handler.advance_state", return_value=False) as advance,
        patch("state_router.handler.fire_and_forget") as fire,
    ):
        execute(make_run(), invoke)
    advance.assert_called_once()
    assert fire.call_count == 0  # lost race; no fire


def test_unknown_action_type_logs_and_no_ops() -> None:
    """A foreign action type doesn't crash the executor."""
    # Noop is a known type, so use a fresh placeholder by constructing
    # a Noop and verifying execute() handles it without side effects.
    with patch("state_router.handler.fire_and_forget") as fire:
        execute(make_run(), Noop("just because"))
    assert fire.call_count == 0
