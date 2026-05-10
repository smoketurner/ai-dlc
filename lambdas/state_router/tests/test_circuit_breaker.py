"""Tests for the per-row dispatch circuit breaker.

The breaker bounds the rollback-redeliver loop that ``execute_invoke_agent``
would otherwise cycle indefinitely on a deterministically failing agent
(e.g., a misconfigured environment variable, a logic bug that raises
before any work is done). The breaker:

* Increments ``dispatch_failure_count`` atomically with the rollback
  whenever a synchronous dispatch fails.
* Reads the counter before each dispatch attempt; suppresses the
  attempt and emits a breaker event when the count is at or above
  :data:`MAX_DISPATCH_FAILURES`.
* Emits ``TASK.BLOCKED`` when the breaker trips on a task that already
  has a PR (humans can comment to retry); otherwise emits
  ``RUN.FAILED`` so the run terminates rather than wedging.
"""

from __future__ import annotations

from unittest.mock import patch

from common.events import EventEnvelope, RunFailed, TaskBlocked
from common.state import RunState, TaskState
from state_router.actions import InvokeAgent
from state_router.circuit_breaker import is_open
from state_router.config import MAX_DISPATCH_FAILURES
from state_router.execute import (
    execute_invoke_agent,
    rollback_after_failure,
)
from state_router.model import Run, Task


def make_run(
    *,
    state: RunState | None = RunState.tasks_in_progress,
    spec_slug: str | None = "demo",
    tasks: tuple[Task, ...] = (),
    dispatch_failure_count: int = 0,
) -> Run:
    """Build a Run carrying the breaker-relevant fields."""
    return Run(
        run_id="r-1",
        correlation_id="c-1",
        project_slug="demo",
        intent="x",
        requestor="alice",
        actor_id="alice",
        current_state=state,
        spec_slug=spec_slug,
        tasks=tasks,
        dispatch_failure_count=dispatch_failure_count,
    )


def make_task(
    *,
    task_id: str = "T-001",
    state: TaskState = TaskState.iterating,
    pr_url: str | None = None,
    dispatch_failure_count: int = 0,
) -> Task:
    return Task(
        task_id=task_id,
        state=state,
        pr_url=pr_url,
        dispatch_failure_count=dispatch_failure_count,
    )


def implementer_invoke(*, task_id: str = "T-001") -> InvokeAgent:
    """InvokeAgent shape the dispatcher emits for the implementer."""
    return InvokeAgent(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/implementer",
        runtime_session_id=f"r-1-{task_id}",
        payload={"task_id": task_id},
        target_pk="RUN#r-1",
        target_sk=f"TASK#{task_id}",
        advance_from=TaskState.iterating.value,
        advance_to=TaskState.implementer_running.value,
    )


def architect_invoke() -> InvokeAgent:
    """InvokeAgent shape the dispatcher emits for the architect (run-level)."""
    return InvokeAgent(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/architect",
        runtime_session_id="r-1-architect",
        payload={},
        target_pk="RUN#r-1",
        target_sk="STATE",
        advance_from=RunState.spec_pending.value,
        advance_to=RunState.architect_running.value,
    )


def advisor_invoke() -> InvokeAgent:
    """Advisor invokes carry no advance fields — gated by an outer GuardedAdvance."""
    return InvokeAgent(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/reviewer",
        runtime_session_id="r-1-T-001-reviewer",
        payload={"task_id": "T-001"},
    )


# ---------------------------------------------------------------------------
# is_open: gating logic
# ---------------------------------------------------------------------------


class TestCircuitBreakerOpen:
    def test_below_threshold_returns_false(self) -> None:
        """A row with count below the limit lets the dispatch proceed."""
        task = make_task(dispatch_failure_count=MAX_DISPATCH_FAILURES - 1)
        run = make_run(tasks=(task,))
        assert is_open(run, implementer_invoke()) is False

    def test_at_threshold_returns_true_and_emits(self) -> None:
        """At the limit, the breaker trips and an event is emitted."""
        task = make_task(
            pr_url="https://github.com/o/r/pull/1",
            dispatch_failure_count=MAX_DISPATCH_FAILURES,
        )
        run = make_run(tasks=(task,))
        with patch("state_router.circuit_breaker.publish") as publish:
            assert is_open(run, implementer_invoke()) is True
        publish.assert_called_once()

    def test_advisor_invoke_skips_breaker(self) -> None:
        """Invokes with no target row are not subject to the per-row breaker.

        Advisor invokes are gated by an outer GuardedAdvance — there's no
        per-row counter to consult, and the rollback path can't increment
        them either.
        """
        run = make_run()
        assert is_open(run, advisor_invoke()) is False

    def test_run_level_at_threshold_trips(self) -> None:
        """Architect / critic dispatches consult the run STATE row counter."""
        run = make_run(
            state=RunState.spec_pending,
            spec_slug=None,
            dispatch_failure_count=MAX_DISPATCH_FAILURES,
        )
        with patch("state_router.circuit_breaker.publish"):
            assert is_open(run, architect_invoke()) is True


# ---------------------------------------------------------------------------
# is_open: emit shape
# ---------------------------------------------------------------------------


class TestBreakerEvent:
    def test_task_with_pr_emits_task_blocked(self) -> None:
        """A task that already has a PR gets TASK.BLOCKED — humans drive recovery."""
        pr = "https://github.com/o/r/pull/1"
        task = make_task(pr_url=pr, dispatch_failure_count=MAX_DISPATCH_FAILURES)
        run = make_run(tasks=(task,))
        with patch("state_router.circuit_breaker.publish") as publish:
            is_open(run, implementer_invoke())
        envelope = publish.call_args.args[0]
        assert isinstance(envelope, EventEnvelope)
        assert envelope.type == "TASK.BLOCKED"
        assert isinstance(envelope.payload, TaskBlocked)
        assert envelope.payload.pr_url == pr
        assert envelope.payload.task_id == "T-001"
        assert "circuit breaker" in envelope.payload.blocked_reason
        assert str(MAX_DISPATCH_FAILURES) in envelope.payload.blocked_reason

    def test_task_without_pr_falls_back_to_run_failed(self) -> None:
        """Without a PR there's no human-recovery surface — fail the run instead.

        ``TaskBlocked.pr_url`` is required because the recovery model is a
        PR comment. When the implementer never produced a PR (first dispatch
        kept failing fast), the breaker falls back to ``RUN.FAILED``.
        """
        task = make_task(pr_url=None, dispatch_failure_count=MAX_DISPATCH_FAILURES)
        run = make_run(tasks=(task,))
        with patch("state_router.circuit_breaker.publish") as publish:
            is_open(run, implementer_invoke())
        envelope = publish.call_args.args[0]
        assert envelope.type == "RUN.FAILED"
        assert isinstance(envelope.payload, RunFailed)
        assert envelope.payload.error_class == "dispatch_circuit_open"

    def test_run_level_emits_run_failed(self) -> None:
        """A run-level dispatch (no task) emits RUN.FAILED with the failed_state."""
        run = make_run(
            state=RunState.spec_pending,
            spec_slug=None,
            dispatch_failure_count=MAX_DISPATCH_FAILURES,
        )
        with patch("state_router.circuit_breaker.publish") as publish:
            is_open(run, architect_invoke())
        envelope = publish.call_args.args[0]
        assert envelope.type == "RUN.FAILED"
        assert envelope.payload.failed_state == RunState.spec_pending.value


# ---------------------------------------------------------------------------
# execute_invoke_agent: integration with the breaker
# ---------------------------------------------------------------------------


class TestExecuteInvokeAgentWithBreaker:
    def test_open_breaker_skips_dispatch(self) -> None:
        """When the breaker is tripped, neither advance nor dispatch fires."""
        task = make_task(
            pr_url="https://github.com/o/r/pull/1",
            dispatch_failure_count=MAX_DISPATCH_FAILURES,
        )
        run = make_run(tasks=(task,))
        with (
            patch("state_router.circuit_breaker.publish"),
            patch("state_router.execute.transactional_advance") as advance,
            patch("state_router.execute.dispatch_to_runtime") as dispatch,
        ):
            execute_invoke_agent(run, implementer_invoke())
        advance.assert_not_called()
        dispatch.assert_not_called()

    def test_closed_breaker_allows_normal_flow(self) -> None:
        """Below the threshold, the existing advance + dispatch + rollback path runs."""
        task = make_task(dispatch_failure_count=0)
        run = make_run(tasks=(task,))
        with (
            patch("state_router.execute.transactional_advance", return_value=True) as advance,
            patch("state_router.execute.dispatch_to_runtime", return_value=True) as dispatch,
        ):
            execute_invoke_agent(run, implementer_invoke())
        advance.assert_called_once()
        dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# rollback: counter increment is atomic with the state reversal
# ---------------------------------------------------------------------------


class TestRollbackIncrement:
    def test_successful_rollback_emits_metric(self) -> None:
        """A successful transactional advance (rollback shape) bumps the failure metric."""
        run = make_run()
        invoke = implementer_invoke()
        with patch(
            "state_router.execute.transactional_advance",
            return_value=True,
        ) as txn:
            rollback_after_failure(run, invoke)
        txn.assert_called_once()
        kwargs = txn.call_args.kwargs
        # Reverse direction (advance_to → advance_from) is the rollback shape.
        assert kwargs["advance_from"] == invoke.advance_to
        assert kwargs["advance_to"] == invoke.advance_from
        assert kwargs["extra_increments"] == {"dispatch_failure_count": 1}
        assert "last_dispatch_failure_at" in kwargs["extra_attrs"]

    def test_rollback_skipped_when_state_already_moved(self) -> None:
        """If the transaction's condition fails, no metric, no error.

        The condition fails when the projector has already advanced the
        state past advance_to (e.g., a stale completion event landed).
        Counting that as a dispatch failure would falsely trip the breaker.
        """
        run = make_run()
        invoke = implementer_invoke()
        with patch(
            "state_router.execute.transactional_advance",
            return_value=False,
        ):
            rollback_after_failure(run, invoke)

    def test_rollback_noop_when_no_advance_fields(self) -> None:
        """Advisor invokes have no state to roll back."""
        run = make_run()
        invoke = advisor_invoke()
        with patch("state_router.execute.transactional_advance") as txn:
            rollback_after_failure(run, invoke)
        txn.assert_not_called()
