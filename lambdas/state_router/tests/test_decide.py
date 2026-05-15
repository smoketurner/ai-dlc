"""Tests for the pure decision function.

The decision function is the structural fix for today's deadlock bug
— it operates on event history rather than a per-state dispatch
table, so adding a new entry point to a state is impossible to forget.
These tests exercise every branch + the replay-safety invariant.
"""

from __future__ import annotations

from typing import Any

import pytest

from state_router.actions import Compound, EmitEvent, InvokeAgent, Noop
from state_router.decide import decide


class Env:
    """Minimal envelope shim matching :class:`EnvelopeLike`."""

    def __init__(
        self,
        *,
        event_type: str,
        event_id: str,
        run_id: str = "run-1",
        correlation_id: str = "corr-1",
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.type = event_type
        self.event_id = event_id
        self.run_id = run_id
        self.correlation_id = correlation_id
        self.payload = payload or {}


def request_received(*, with_issue: bool = True) -> Env:
    """Build a REQUEST.RECEIVED with optional source-issue context."""
    payload: dict[str, Any] = {"project_slug": "demo", "requestor": "alice"}
    if with_issue:
        payload["source_issue_url"] = "https://github.com/x/y/issues/1"
    return Env(event_type="REQUEST.RECEIVED", event_id="evt-1", payload=payload)


def issue_triaged(*, action: str) -> Env:
    return Env(
        event_type="ISSUE.TRIAGED",
        event_id="evt-2",
        payload={"project_slug": "demo", "action": action},
    )


def design_ready() -> Env:
    return Env(event_type="DESIGN.READY", event_id="evt-3", payload={"project_slug": "demo"})


def impl_pr_opened() -> Env:
    return Env(
        event_type="IMPL_PR.OPENED",
        event_id="evt-4",
        payload={
            "project_slug": "demo",
            "pr_url": "https://github.com/x/y/pull/1",
        },
    )


def dispatched(agent: str, event_id: str = "dispatch-1") -> Env:
    type_map = {
        "triage": "TRIAGE.DISPATCHED",
        "architect": "ARCHITECT.DISPATCHED",
        "implementer": "IMPLEMENTER.DISPATCHED",
        "validators": "VALIDATORS.DISPATCHED",
        "proposer": "PROPOSER.DISPATCHED",
    }
    return Env(event_type=type_map[agent], event_id=event_id, payload={"project_slug": "demo"})


def test_no_events_returns_noop() -> None:
    assert isinstance(decide([]), Noop)


def test_terminal_event_returns_noop() -> None:
    events = [
        request_received(),
        Env(event_type="RUN.COMPLETED", event_id="evt-x", payload={"project_slug": "demo"}),
    ]
    assert isinstance(decide(events), Noop)


def test_request_received_with_issue_dispatches_triage() -> None:
    action = decide([request_received()])
    assert isinstance(action, InvokeAgent)
    assert action.agent == "triage"


def test_request_received_programmatic_dispatches_architect() -> None:
    """No source_issue_url means programmatic run — skip triage."""
    action = decide([request_received(with_issue=False)])
    assert isinstance(action, InvokeAgent)
    assert action.agent == "architect"


def test_triage_dispatched_marker_blocks_redispatch() -> None:
    """Marker presence proves the agent's already been kicked off."""
    events = [request_received(), dispatched("triage")]
    assert isinstance(decide(events), Noop)


def test_proceed_dispatches_architect_after_triage() -> None:
    events = [request_received(), dispatched("triage"), issue_triaged(action="proceed")]
    action = decide(events)
    assert isinstance(action, InvokeAgent)
    assert action.agent == "architect"


def test_architect_dispatched_marker_blocks_redispatch() -> None:
    events = [
        request_received(),
        dispatched("triage"),
        issue_triaged(action="proceed"),
        dispatched("architect"),
    ]
    assert isinstance(decide(events), Noop)


def test_design_ready_dispatches_implementer() -> None:
    events = [
        request_received(),
        dispatched("triage"),
        issue_triaged(action="proceed"),
        dispatched("architect"),
        design_ready(),
    ]
    action = decide(events)
    assert isinstance(action, InvokeAgent)
    assert action.agent == "implementer"
    assert action.mode == "implementation"


def test_pr_open_no_signal_returns_noop() -> None:
    """Open PR + nothing else → wait for human."""
    events = [
        request_received(),
        dispatched("triage"),
        issue_triaged(action="proceed"),
        dispatched("architect"),
        design_ready(),
        dispatched("implementer"),
        impl_pr_opened(),
    ]
    assert isinstance(decide(events), Noop)


def test_iteration_request_dispatches_implementer_revision() -> None:
    """``IMPL.ITERATION_REQUESTED`` triggers a fresh implementer revision."""
    events = [
        request_received(),
        dispatched("triage"),
        issue_triaged(action="proceed"),
        dispatched("architect"),
        design_ready(),
        dispatched("implementer"),
        impl_pr_opened(),
        Env(
            event_type="IMPL.ITERATION_REQUESTED",
            event_id="evt-it",
            payload={
                "project_slug": "demo",
                "source": "issue_comment_mention",
                "feedback_body": "please rename Foo",
                "commenter": "alice",
                "comment_id": 1,
            },
        ),
    ]
    action = decide(events)
    assert isinstance(action, InvokeAgent)
    assert action.agent == "implementer"
    assert action.mode == "revision"


def test_validation_request_dispatches_validators() -> None:
    """``VALIDATION.REQUESTED`` (human asked) triggers reviewer + tester + code_critic."""
    events = [
        request_received(),
        dispatched("triage"),
        issue_triaged(action="proceed"),
        dispatched("architect"),
        design_ready(),
        dispatched("implementer"),
        impl_pr_opened(),
        Env(
            event_type="VALIDATION.REQUESTED",
            event_id="evt-v",
            payload={
                "project_slug": "demo",
                "pr_url": "https://github.com/x/y/pull/1",
                "delivery_id": "d-1",
                "commenter": "alice",
            },
        ),
    ]
    action = decide(events)
    assert isinstance(action, InvokeAgent)
    assert action.agent == "validators"


def test_validators_dispatched_marker_blocks_redispatch() -> None:
    events = [
        request_received(),
        dispatched("triage"),
        issue_triaged(action="proceed"),
        dispatched("architect"),
        design_ready(),
        dispatched("implementer"),
        impl_pr_opened(),
        Env(
            event_type="VALIDATION.REQUESTED",
            event_id="evt-v",
            payload={
                "project_slug": "demo",
                "pr_url": "https://github.com/x/y/pull/1",
                "delivery_id": "d-1",
                "commenter": "alice",
            },
        ),
        Env(
            event_type="VALIDATORS.DISPATCHED",
            event_id="evt-vd",
            payload={"project_slug": "demo", "pr_url": "x", "revision_number": 0},
        ),
    ]
    assert isinstance(decide(events), Noop)


@pytest.mark.parametrize("action", ["ask", "defer", "decline"])
def test_triage_cancel_outcomes_emit_run_cancel(action: str) -> None:
    """All three cancel paths emit ``RUN.CANCEL_REQUESTED``."""
    events = [request_received(), dispatched("triage"), issue_triaged(action=action)]
    result = decide(events)
    assert isinstance(result, Compound)
    assert any(
        isinstance(sub, EmitEvent) and sub.envelope.type == "RUN.CANCEL_REQUESTED"
        for sub in result.actions
    )


def test_triage_research_dispatches_proposer() -> None:
    events = [request_received(), dispatched("triage"), issue_triaged(action="research")]
    action = decide(events)
    assert isinstance(action, InvokeAgent)
    assert action.agent == "proposer"


def test_replay_safety_pure_function() -> None:
    """The same event history produces the same action every time."""
    events = [
        request_received(),
        dispatched("triage"),
        issue_triaged(action="proceed"),
    ]
    first = decide(events)
    second = decide(events)
    assert isinstance(first, InvokeAgent)
    assert isinstance(second, InvokeAgent)
    assert first.agent == second.agent == "architect"
