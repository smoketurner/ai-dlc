"""Pure-function tests for the state_router dispatch table.

Every entry in :data:`RunState` has a row here. A regression that drops
a dispatch entry surfaces as an unmapped state → ``decide`` returns a
``Noop`` carrying ``unknown run state``; the coverage guard at the
bottom catches that.
"""

from __future__ import annotations

import pytest

from common.state import RunState
from state_router.actions import (
    AdvanceState,
    CompoundAction,
    EmitEvent,
    InvokeAgent,
    InvokeRepoHelper,
    Noop,
)
from state_router.dispatch import RUN_DISPATCH, decide
from state_router.model import Run

PR_URL = "https://github.com/owner/repo/pull/9"
ISSUE_URL = "https://github.com/owner/repo/issues/7"
PLAN_KEY = "runs/r-1/plan.md"
CRITIQUE_KEY = "runs/r-1/critique.md"


def make_run(  # noqa: PLR0913
    *,
    state: RunState | None,
    triage_action: str | None = None,
    source_issue_url: str | None = None,
    source_issue_title: str | None = None,
    source_issue_body: str | None = None,
    issue_number: int | None = None,
    issue_title: str | None = None,
    issue_body: str | None = None,
    issue_labels: tuple[str, ...] = (),
    target_repo: str | None = "owner/repo",
    triggering_comment_body: str | None = None,
    plan_s3_key: str | None = None,
    critique_s3_key: str | None = None,
    pr_url: str | None = None,
    reviewer_verdict: str = "",
    check_state: str = "",
    pending_revision_feedback: tuple[dict, ...] = (),
    revision_count: int = 0,
    last_revision_trigger: str = "",
) -> Run:
    """Build a Run with sane defaults for tests."""
    return Run(
        run_id="r-1",
        correlation_id="c-1",
        project_slug="demo",
        intent="x",
        requestor="alice",
        actor_id="alice",
        current_state=state,
        triage_action=triage_action,
        target_repo=target_repo,
        source_issue_url=source_issue_url,
        source_issue_title=source_issue_title,
        source_issue_body=source_issue_body,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        issue_labels=issue_labels,
        triggering_comment_body=triggering_comment_body,
        plan_s3_key=plan_s3_key,
        critique_s3_key=critique_s3_key,
        pr_url=pr_url,
        reviewer_verdict=reviewer_verdict,
        check_state=check_state,
        pending_revision_feedback=pending_revision_feedback,
        revision_count=revision_count,
        last_revision_trigger=last_revision_trigger,
    )


# ---------------------------------------------------------------------------
# received → triage / architect
# ---------------------------------------------------------------------------


class TestRunReceived:
    def test_received_with_issue_invokes_triage(self) -> None:
        run = make_run(
            state=RunState.received,
            source_issue_url=ISSUE_URL,
            issue_number=1,
            issue_title="bug: foo",
            issue_body="describe",
        )
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "triage" in action.runtime_arn
        assert action.advance_to == RunState.triaging.value
        assert action.payload["issue_number"] == 1
        assert action.payload["issue_title"] == "bug: foo"
        assert action.payload["issue_body"] == "describe"
        assert action.payload["triggering_comment_body"] is None

    def test_received_with_triggering_comment_strips_bot_mention(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``@aidlc-bot please reconsider X`` reaches triage with the bot mention stripped."""
        monkeypatch.setenv("AIDLC_GITHUB_BOT_LOGIN", "ai-dlc[bot]")
        run = make_run(
            state=RunState.received,
            source_issue_url=ISSUE_URL,
            issue_number=1,
            issue_title="bug: foo",
            issue_body="describe",
            triggering_comment_body="@ai-dlc[bot] please reconsider — needs a 503 path",
        )
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert action.payload["triggering_comment_body"] == "please reconsider — needs a 503 path"

    def test_received_with_issue_url_only_is_noop(self) -> None:
        run = make_run(state=RunState.received, source_issue_url=ISSUE_URL)
        action = decide(run)
        assert isinstance(action, Noop)

    def test_received_without_issue_invokes_architect(self) -> None:
        run = make_run(state=RunState.received)
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "architect" in action.runtime_arn
        assert action.advance_to == RunState.architect_running.value
        assert action.payload["triggering_comment_body"] is None

    def test_received_without_issue_threads_triggering_comment_to_architect(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the architect runs directly, the same comment threading applies."""
        monkeypatch.setenv("AIDLC_GITHUB_BOT_LOGIN", "ai-dlc[bot]")
        run = make_run(
            state=RunState.received,
            triggering_comment_body="@ai-dlc[bot] add a feature flag",
        )
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "architect" in action.runtime_arn
        assert action.payload["triggering_comment_body"] == "add a feature flag"

    def test_received_noop_when_runtime_arn_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AIDLC_ARCHITECT_RUNTIME_ARN")
        run = make_run(state=RunState.received)
        action = decide(run)
        assert isinstance(action, Noop)


# ---------------------------------------------------------------------------
# Waiting states
# ---------------------------------------------------------------------------


class TestRunWaitingStates:
    @pytest.mark.parametrize(
        "state",
        [
            RunState.triaging,
            RunState.architect_running,
            RunState.critic_running,
            RunState.implementer_running,
            RunState.validation_running,
            RunState.awaiting_checks,
            RunState.awaiting_human_merge,
            RunState.proposer_running,
        ],
    )
    def test_waiting_states_return_noop(self, state: RunState) -> None:
        run = make_run(state=state)
        action = decide(run)
        assert isinstance(action, Noop)


# ---------------------------------------------------------------------------
# triage_decided → architect / proposer / cancel
# ---------------------------------------------------------------------------


class TestRunTriageDecided:
    def test_proceed_invokes_architect(self) -> None:
        run = make_run(
            state=RunState.triage_decided,
            triage_action="proceed",
            source_issue_url=ISSUE_URL,
            source_issue_title="add X",
            source_issue_body="we need X because Y",
        )
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "architect" in action.runtime_arn
        assert action.advance_to == RunState.architect_running.value
        assert action.payload["source_issue_url"] == ISSUE_URL
        assert action.payload["source_issue_title"] == "add X"
        assert action.payload["source_issue_body"] == "we need X because Y"

    def test_research_invokes_proposer(self) -> None:
        run = make_run(
            state=RunState.triage_decided,
            triage_action="research",
            issue_body="please review:\n- https://example.com/post-a",
        )
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "proposer" in action.runtime_arn
        assert action.payload["trigger_reason"] == "research"
        assert action.advance_to == RunState.proposer_running.value
        assert "https://example.com/post-a" in action.payload["intent"]

    def test_ask_emits_cancel_with_comment_and_label(self) -> None:
        run = make_run(
            state=RunState.triage_decided,
            triage_action="ask",
            source_issue_url=ISSUE_URL,
            issue_number=1,
        )
        action = decide(run)
        assert isinstance(action, CompoundAction)
        helpers = [a for a in action.actions if isinstance(a, InvokeRepoHelper)]
        ops = [h.op for h in helpers]
        assert "comment_issue" in ops
        assert "label_issue" in ops
        emits = [a for a in action.actions if isinstance(a, EmitEvent)]
        assert len(emits) == 1
        assert emits[0].envelope.type == "RUN.CANCEL_REQUESTED"

    @pytest.mark.parametrize(
        ("triage_action", "label"),
        [("defer", "aidlc:deferred"), ("decline", "aidlc:declined")],
    )
    def test_defer_decline_label_issue_and_cancel(
        self,
        triage_action: str,
        label: str,
    ) -> None:
        run = make_run(
            state=RunState.triage_decided,
            triage_action=triage_action,
            source_issue_url=ISSUE_URL,
            issue_number=1,
        )
        action = decide(run)
        assert isinstance(action, CompoundAction)
        labels = [
            a for a in action.actions if isinstance(a, InvokeRepoHelper) and a.op == "label_issue"
        ]
        assert len(labels) == 1
        assert labels[0].args["labels"] == [label]
        emits = [a for a in action.actions if isinstance(a, EmitEvent)]
        assert len(emits) == 1
        assert emits[0].envelope.type == "RUN.CANCEL_REQUESTED"

    def test_unknown_action_is_noop(self) -> None:
        run = make_run(state=RunState.triage_decided, triage_action="frob")
        action = decide(run)
        assert isinstance(action, Noop)


# ---------------------------------------------------------------------------
# designed → critic
# ---------------------------------------------------------------------------


class TestRunDesigned:
    def test_designed_invokes_critic_with_plan_key(self) -> None:
        run = make_run(
            state=RunState.designed,
            plan_s3_key=PLAN_KEY,
            source_issue_url=ISSUE_URL,
            source_issue_title="add X",
            source_issue_body="we need X",
        )
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "critic" in action.runtime_arn
        assert action.payload["plan_s3_key"] == PLAN_KEY
        assert action.advance_from == RunState.designed.value
        assert action.advance_to == RunState.critic_running.value
        assert action.payload["source_issue_url"] == ISSUE_URL
        assert action.payload["source_issue_title"] == "add X"

    def test_designed_without_plan_is_noop(self) -> None:
        run = make_run(state=RunState.designed)
        action = decide(run)
        assert isinstance(action, Noop)


# ---------------------------------------------------------------------------
# critiqued → implementer (first pass)
# ---------------------------------------------------------------------------


class TestRunCritiqued:
    def test_critiqued_invokes_implementer_implementation_mode(self) -> None:
        run = make_run(
            state=RunState.critiqued,
            plan_s3_key=PLAN_KEY,
            critique_s3_key=CRITIQUE_KEY,
            source_issue_url=ISSUE_URL,
            source_issue_title="add X",
        )
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "implementer" in action.runtime_arn
        assert action.payload["mode"] == "implementation"
        assert action.payload["plan_s3_key"] == PLAN_KEY
        assert action.payload["critique_s3_key"] == CRITIQUE_KEY
        assert action.payload["source_issue_url"] == ISSUE_URL
        assert action.payload["source_issue_title"] == "add X"
        assert "intent" in action.payload
        assert action.payload["revision_number"] == 0
        assert action.advance_to == RunState.implementer_running.value

    def test_critiqued_without_plan_is_noop(self) -> None:
        run = make_run(state=RunState.critiqued)
        action = decide(run)
        assert isinstance(action, Noop)


# ---------------------------------------------------------------------------
# impl_pr_open → validators in parallel
# ---------------------------------------------------------------------------


class TestRunImplPrOpen:
    def test_dispatches_validators_in_parallel_and_advances(self) -> None:
        run = make_run(
            state=RunState.impl_pr_open,
            plan_s3_key=PLAN_KEY,
            pr_url=PR_URL,
            source_issue_url=ISSUE_URL,
            source_issue_title="bug: foo",
            source_issue_body="repro steps",
        )
        action = decide(run)
        assert isinstance(action, CompoundAction)
        invokes = [a for a in action.actions if isinstance(a, InvokeAgent)]
        runtimes = {invoke.runtime_arn for invoke in invokes}
        assert any("reviewer" in arn for arn in runtimes)
        assert any("tester" in arn for arn in runtimes)
        assert any("code_critic" in arn for arn in runtimes)
        advances = [a for a in action.actions if isinstance(a, AdvanceState)]
        assert len(advances) == 1
        assert advances[0].advance_from == RunState.impl_pr_open.value
        assert advances[0].advance_to == RunState.validation_running.value

    def test_code_critic_receives_source_issue_context(self) -> None:
        """Code-critic specifically reviews against the original GitHub issue."""
        run = make_run(
            state=RunState.impl_pr_open,
            plan_s3_key=PLAN_KEY,
            pr_url=PR_URL,
            source_issue_url=ISSUE_URL,
            source_issue_title="bug: foo",
            source_issue_body="repro steps",
        )
        action = decide(run)
        assert isinstance(action, CompoundAction)
        invokes = [a for a in action.actions if isinstance(a, InvokeAgent)]
        cc = next((i for i in invokes if "code_critic" in i.runtime_arn), None)
        assert cc is not None
        assert cc.payload["source_issue_url"] == ISSUE_URL
        assert cc.payload["source_issue_title"] == "bug: foo"
        assert cc.payload["source_issue_body"] == "repro steps"
        # reviewer + tester do NOT receive the source issue context.
        reviewer = next((i for i in invokes if "reviewer" in i.runtime_arn), None)
        assert reviewer is not None
        assert "source_issue_url" not in reviewer.payload

    def test_noop_when_pr_url_not_yet_projected(self) -> None:
        run = make_run(state=RunState.impl_pr_open, plan_s3_key=PLAN_KEY, pr_url=None)
        action = decide(run)
        assert isinstance(action, Noop)

    def test_noop_when_plan_missing(self) -> None:
        run = make_run(state=RunState.impl_pr_open, pr_url=PR_URL, plan_s3_key=None)
        action = decide(run)
        assert isinstance(action, Noop)


# ---------------------------------------------------------------------------
# validation_complete — verdict + checks branching
# ---------------------------------------------------------------------------


class TestRunValidationComplete:
    @pytest.mark.parametrize("verdict", ["approve", "comment"])
    def test_approve_with_checks_passed_advances_to_awaiting_human_merge(
        self,
        verdict: str,
    ) -> None:
        run = make_run(
            state=RunState.validation_complete,
            plan_s3_key=PLAN_KEY,
            pr_url=PR_URL,
            reviewer_verdict=verdict,
            check_state="passed",
        )
        action = decide(run)
        assert isinstance(action, AdvanceState)
        assert action.advance_from == RunState.validation_complete.value
        assert action.advance_to == RunState.awaiting_human_merge.value

    @pytest.mark.parametrize("verdict", ["approve", "comment"])
    def test_approve_with_checks_failed_advances_into_revising(self, verdict: str) -> None:
        """The state-router only advances; the follow-up beacon fires the implementer.

        Prevents the historical double-dispatch where both this handler
        and ``handle_revising`` invoked the implementer on the same
        revision.
        """
        run = make_run(
            state=RunState.validation_complete,
            plan_s3_key=PLAN_KEY,
            pr_url=PR_URL,
            reviewer_verdict=verdict,
            check_state="failed",
            pending_revision_feedback=({"kind": "ci_failure"},),
        )
        action = decide(run)
        assert isinstance(action, AdvanceState)
        assert action.advance_from == RunState.validation_complete.value
        assert action.advance_to == RunState.revising.value
        assert ("last_revision_trigger", "ci_failure") in action.extra_attrs

    @pytest.mark.parametrize("verdict", ["approve", "comment"])
    def test_approve_with_checks_pending_advances_to_awaiting_checks(
        self,
        verdict: str,
    ) -> None:
        """Reviewer approved but checks haven't projected yet — park on awaiting_checks."""
        run = make_run(
            state=RunState.validation_complete,
            plan_s3_key=PLAN_KEY,
            pr_url=PR_URL,
            reviewer_verdict=verdict,
            check_state="",
        )
        action = decide(run)
        assert isinstance(action, AdvanceState)
        assert action.advance_to == RunState.awaiting_checks.value

    def test_empty_verdict_treated_as_approve_pending(self) -> None:
        """Empty verdict falls through the approve branch (defensive)."""
        run = make_run(
            state=RunState.validation_complete,
            plan_s3_key=PLAN_KEY,
            pr_url=PR_URL,
            reviewer_verdict="",
            check_state="",
        )
        action = decide(run)
        assert isinstance(action, AdvanceState)
        assert action.advance_to == RunState.awaiting_checks.value

    def test_request_changes_advances_into_revising(self) -> None:
        """``request_changes`` advances to revising; ``handle_revising`` fires the implementer."""
        run = make_run(
            state=RunState.validation_complete,
            plan_s3_key=PLAN_KEY,
            pr_url=PR_URL,
            reviewer_verdict="request_changes",
            revision_count=0,
        )
        action = decide(run)
        assert isinstance(action, AdvanceState)
        assert action.advance_from == RunState.validation_complete.value
        assert action.advance_to == RunState.revising.value
        assert ("last_revision_trigger", "reviewer_request_changes") in action.extra_attrs

    def test_request_changes_advances_even_at_cap(self) -> None:
        """Cap is checked in ``handle_revising``, not here.

        After advancing to ``revising`` the follow-up beacon hits the
        cap and emits ``RUN.FAILED`` — see :class:`TestRunRevising`.
        """
        run = make_run(
            state=RunState.validation_complete,
            plan_s3_key=PLAN_KEY,
            pr_url=PR_URL,
            reviewer_verdict="request_changes",
            revision_count=3,
        )
        action = decide(run)
        assert isinstance(action, AdvanceState)
        assert action.advance_to == RunState.revising.value

    def test_unknown_verdict_is_noop(self) -> None:
        run = make_run(
            state=RunState.validation_complete,
            reviewer_verdict="not-a-verdict",
        )
        action = decide(run)
        assert isinstance(action, Noop)


# ---------------------------------------------------------------------------
# revising → implementer (mode=revision)
# ---------------------------------------------------------------------------


class TestRunRevising:
    def test_revising_dispatches_implementer_with_feedback(self) -> None:
        """``revising`` was reached via IMPL.ITERATION_REQUESTED — human-mention path."""
        run = make_run(
            state=RunState.revising,
            plan_s3_key=PLAN_KEY,
            critique_s3_key=CRITIQUE_KEY,
            pr_url=PR_URL,
            source_issue_url=ISSUE_URL,
            source_issue_title="add X",
            pending_revision_feedback=(
                {
                    "kind": "issue_comment_mention",
                    "comment_id": 42,
                    "body": "please add a 503 path",
                    "commenter": "alice",
                },
            ),
            revision_count=1,
            last_revision_trigger="human_mention",
        )
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert "implementer" in action.runtime_arn
        assert action.payload["mode"] == "revision"
        assert action.payload["plan_s3_key"] == PLAN_KEY
        assert action.payload["critique_s3_key"] == CRITIQUE_KEY
        assert action.payload["pr_url"] == PR_URL
        assert action.payload["source_issue_url"] == ISSUE_URL
        assert action.payload["source_issue_title"] == "add X"
        assert "intent" in action.payload
        # next pass = revision_count + 1
        assert action.payload["revision_number"] == 2
        assert action.payload["revision_feedback"] == [
            {
                "kind": "issue_comment_mention",
                "comment_id": 42,
                "body": "please add a 503 path",
                "commenter": "alice",
            },
        ]

    @pytest.mark.parametrize(
        "trigger",
        ["reviewer_request_changes", "ci_failure"],
    )
    def test_revising_fails_run_when_cap_hit_on_automated_trigger(self, trigger: str) -> None:
        run = make_run(
            state=RunState.revising,
            plan_s3_key=PLAN_KEY,
            pr_url=PR_URL,
            revision_count=3,
            last_revision_trigger=trigger,
        )
        action = decide(run)
        assert isinstance(action, EmitEvent)
        assert action.envelope.type == "RUN.FAILED"

    def test_revising_dispatches_at_cap_for_human_mention(self) -> None:
        """Human-mention revisions are uncapped — even at MAX_REVISIONS we dispatch."""
        run = make_run(
            state=RunState.revising,
            plan_s3_key=PLAN_KEY,
            pr_url=PR_URL,
            revision_count=3,
            last_revision_trigger="human_mention",
        )
        action = decide(run)
        assert isinstance(action, InvokeAgent)
        assert action.payload["revision_number"] == 4

    def test_revising_is_noop_when_implementer_arn_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AIDLC_IMPLEMENTER_RUNTIME_ARN")
        run = make_run(state=RunState.revising, plan_s3_key=PLAN_KEY, pr_url=PR_URL)
        action = decide(run)
        assert isinstance(action, Noop)


# ---------------------------------------------------------------------------
# Terminal states
# ---------------------------------------------------------------------------


class TestRunTerminalStates:
    @pytest.mark.parametrize(
        "state",
        [RunState.done, RunState.failed, RunState.cancelled],
    )
    def test_terminal_states_are_noop(self, state: RunState) -> None:
        run = make_run(state=state)
        action = decide(run)
        assert isinstance(action, Noop)

    def test_unset_state_is_noop(self) -> None:
        run = make_run(state=None)
        action = decide(run)
        assert isinstance(action, Noop)


# ---------------------------------------------------------------------------
# Coverage guard
# ---------------------------------------------------------------------------


class TestDispatchTablesCover:
    def test_every_run_state_has_a_dispatch_handler(self) -> None:
        """Every RunState must be in the table — no silent gaps."""
        for state in RunState:
            assert state in RUN_DISPATCH, f"RunState.{state.name} missing"
