"""Tests for the per-row dispatch circuit breaker.

The breaker bounds the rollback-redeliver loop that ``execute_invoke_agent``
would otherwise cycle indefinitely on a deterministically failing agent
(e.g., a misconfigured environment variable, a logic bug that raises
before any work is done). The breaker:

* Increments ``dispatch_failure_count`` atomically with the rollback
  whenever a synchronous dispatch fails.
* Reads the counter before each dispatch attempt; suppresses the
  attempt and emits ``RUN.FAILED`` when the count is at or above
  :data:`MAX_DISPATCH_FAILURES`.
"""

from __future__ import annotations

from unittest.mock import patch

from common.events import EventEnvelope, RunFailed
from common.state import RunState
from state_router.actions import InvokeAgent, InvokeRepoHelper
from state_router.circuit_breaker import is_open
from state_router.config import MAX_DISPATCH_FAILURES
from state_router.execute import (
    execute_invoke_agent,
    execute_invoke_repo_helper,
    rollback_after_failure,
)
from state_router.model import Run


def make_run(
    *,
    state: RunState | None = RunState.critiqued,
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
        dispatch_failure_count=dispatch_failure_count,
    )


def implementer_invoke() -> InvokeAgent:
    """InvokeAgent shape ``handle_critiqued`` returns for the implementer."""
    return InvokeAgent(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/implementer",
        runtime_session_id="r-1-impl",
        runtime_user_id="gh:tester",
        payload={"mode": "implementation"},
        target_pk="RUN#r-1",
        target_sk="STATE",
        advance_from=RunState.critiqued.value,
        advance_to=RunState.implementer_running.value,
    )


def validator_invoke() -> InvokeAgent:
    """Validator invokes carry no advance fields — gated by the surrounding compound."""
    return InvokeAgent(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/reviewer",
        runtime_session_id="r-1-reviewer-r0",
        runtime_user_id="gh:tester",
        payload={"pr_url": "..."},
    )


def comment_pr_invoke() -> InvokeRepoHelper:
    """A run-level repo_helper invoke with state advance."""
    return InvokeRepoHelper(
        op="comment_pr",
        args={"repo": "o/r", "pr_number": 1, "body": "..."},
        target_pk="RUN#r-1",
        target_sk="STATE",
        advance_from=RunState.validation_complete.value,
        advance_to=RunState.awaiting_human_merge.value,
    )


# ---------------------------------------------------------------------------
# is_open: gating logic
# ---------------------------------------------------------------------------


class TestCircuitBreakerOpen:
    def test_below_threshold_returns_false(self) -> None:
        """A row with count below the limit lets the dispatch proceed."""
        run = make_run(dispatch_failure_count=MAX_DISPATCH_FAILURES - 1)
        assert is_open(run, implementer_invoke()) is False

    def test_at_threshold_returns_true_and_emits(self) -> None:
        """At the limit, the breaker trips and an event is emitted."""
        run = make_run(dispatch_failure_count=MAX_DISPATCH_FAILURES)
        with patch("state_router.circuit_breaker.publish") as publish:
            assert is_open(run, implementer_invoke()) is True
        publish.assert_called_once()

    def test_validator_invoke_skips_breaker(self) -> None:
        """Invokes with no target row are not subject to the per-row breaker.

        Validator invokes are dispatched as a fan-out under a single
        AdvanceState — there's no per-row counter to consult, and the
        rollback path can't increment them either.
        """
        run = make_run()
        assert is_open(run, validator_invoke()) is False

    def test_repo_helper_invoke_at_threshold_trips(self) -> None:
        """``comment_pr`` dispatch reads the same per-row counter as the agent path."""
        run = make_run(
            state=RunState.validation_complete,
            dispatch_failure_count=MAX_DISPATCH_FAILURES,
        )
        with patch("state_router.circuit_breaker.publish"):
            assert is_open(run, comment_pr_invoke()) is True


# ---------------------------------------------------------------------------
# is_open: emit shape
# ---------------------------------------------------------------------------


class TestBreakerEvent:
    def test_emits_run_failed_with_failed_state(self) -> None:
        """The breaker always emits RUN.FAILED with the current state."""
        run = make_run(
            state=RunState.critiqued,
            dispatch_failure_count=MAX_DISPATCH_FAILURES,
        )
        with patch("state_router.circuit_breaker.publish") as publish:
            is_open(run, implementer_invoke())
        envelope = publish.call_args.args[0]
        assert isinstance(envelope, EventEnvelope)
        assert envelope.type == "RUN.FAILED"
        assert isinstance(envelope.payload, RunFailed)
        assert envelope.payload.failed_state == RunState.critiqued.value
        assert envelope.payload.error_class == "dispatch_circuit_open"


# ---------------------------------------------------------------------------
# execute_invoke_agent: integration with the breaker
# ---------------------------------------------------------------------------


class TestExecuteInvokeAgentWithBreaker:
    def test_open_breaker_skips_dispatch(self) -> None:
        """When the breaker is tripped, neither advance nor dispatch fires."""
        run = make_run(dispatch_failure_count=MAX_DISPATCH_FAILURES)
        with (
            patch("state_router.circuit_breaker.publish"),
            patch("state_router.execute.transactional_advance") as advance,
            patch("state_router.execute.dispatch_to_runtime") as dispatch,
        ):
            execute_invoke_agent(run, implementer_invoke())
        advance.assert_not_called()
        dispatch.assert_not_called()

    def test_closed_breaker_allows_normal_flow(self) -> None:
        """Below the threshold, the existing advance + dispatch path runs."""
        run = make_run(dispatch_failure_count=0)
        with (
            patch("state_router.execute.transactional_advance", return_value=True) as advance,
            patch("state_router.execute.dispatch_to_runtime", return_value=True) as dispatch,
        ):
            execute_invoke_agent(run, implementer_invoke())
        advance.assert_called_once()
        dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# execute_invoke_repo_helper: integration with the breaker
# ---------------------------------------------------------------------------


class TestExecuteInvokeRepoHelperWithBreaker:
    def test_open_breaker_skips_repo_helper_invoke(self) -> None:
        """When the breaker is tripped, neither the Lambda invoke nor advance fires."""
        run = make_run(
            state=RunState.validation_complete,
            dispatch_failure_count=MAX_DISPATCH_FAILURES,
        )
        with (
            patch("state_router.circuit_breaker.publish"),
            patch("state_router.execute.lambda_client") as lambda_client_mock,
            patch("state_router.execute.transactional_advance") as advance,
        ):
            execute_invoke_repo_helper(run, comment_pr_invoke())
        lambda_client_mock.assert_not_called()
        advance.assert_not_called()


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
        """Validator invokes have no state to roll back."""
        run = make_run()
        invoke = validator_invoke()
        with patch("state_router.execute.transactional_advance") as txn:
            rollback_after_failure(run, invoke)
        txn.assert_not_called()
