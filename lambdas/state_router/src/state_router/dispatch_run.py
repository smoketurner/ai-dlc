"""Run-state dispatch — pure functions ``Run -> Action``.

Each handler decides the next action for one run state. No side
effects: the executor in :mod:`state_router.execute` consumes the
returned action and applies it.

The handler set is mostly 1:1 with :class:`~common.state.RunState`
entries; states that wait on external events map to
:func:`noop_waiting` and terminal states to :func:`terminal`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from common.events import (
    EventEnvelope,
    RunCancelRequested,
    RunFailed,
)
from common.github_mentions import strip_bot_mention
from common.ids import CorrelationId, RunId, new_event_id
from common.state import RunState
from state_router.actions import (
    Action,
    AdvanceState,
    CompoundAction,
    EmitEvent,
    InvokeAgent,
    InvokeRepoHelper,
    Noop,
)
from state_router.config import (
    github_bot_login,
    runtime_arn,
)

if TYPE_CHECKING:
    from state_router.model import Run

type RunHandler = Callable[["Run"], Action]


MAX_REVISIONS = 3
"""Upper bound on automated revision cycles before the run fails.

Caps the agent-loop blast radius: if the reviewer keeps rejecting the
implementer's fixes, or CI keeps going red, the run fails into the
human's lap rather than spending tokens indefinitely.

Counts: validator-driven (``request_changes``) and CI-driven
(``CHECKS.FAILED``) revisions. Does not count: human ``@aidlc-bot``
mentions (the human is actively steering, so the cap doesn't apply).
"""


# ---------------------------------------------------------------------------
# received → triage (or straight to architect)
# ---------------------------------------------------------------------------


def handle_received(run: Run) -> Action:
    """Branch on whether the run was triggered by a GitHub issue.

    Issue-driven runs go through triage first; programmatic runs (POST
    /v1/runs without ``source_issue_url``) skip straight to the
    architect.
    """
    arn = runtime_arn("triage" if run.source_issue_url else "architect")
    if not arn:
        return Noop("runtime ARN not yet provisioned")
    if run.source_issue_url:
        return invoke_triage(run, arn)
    return invoke_architect(run, arn, advance_from=RunState.received)


def invoke_triage(run: Run, arn: str) -> Action:
    """Dispatch the triage agent and advance to ``triaging``.

    ``TriageInput`` requires every field below; if the row is missing
    the issue number/title/body/labels (e.g., the trigger preceded the
    issue-context plumbing), Noop and let an operator fix the row
    rather than dispatching an agent that will only ``ValidationError``.
    """
    if (
        not run.target_repo
        or not run.source_issue_url
        or run.issue_number is None
        or not run.issue_title
    ):
        return Noop("triage: STATE row missing issue context")
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-triage",
        payload={
            "project_slug": run.project_slug,
            "target_repo": run.target_repo,
            "issue_url": run.source_issue_url,
            "issue_number": run.issue_number,
            "issue_title": run.issue_title,
            "issue_body": run.issue_body or "",
            "issue_labels": list(run.issue_labels),
            "triggering_comment_body": strip_bot_mention(
                run.triggering_comment_body,
                github_bot_login(),
            ),
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": run.actor_id,
            "requestor_sub": run.requestor_sub,
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk="STATE",
        advance_from=RunState.received.value,
        advance_to=RunState.triaging.value,
    )


def invoke_architect(run: Run, arn: str, *, advance_from: RunState) -> InvokeAgent:
    """Dispatch the architect agent and advance to ``architect_running``."""
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-architect",
        payload={
            "project_slug": run.project_slug,
            "intent": run.intent,
            "triggering_comment_body": strip_bot_mention(
                run.triggering_comment_body,
                github_bot_login(),
            ),
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": run.actor_id,
            "requestor_sub": run.requestor_sub,
            "target_repo": run.target_repo,
            "source_issue_url": run.source_issue_url,
            "source_issue_title": run.source_issue_title or run.issue_title,
            "source_issue_body": run.source_issue_body or run.issue_body,
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk="STATE",
        advance_from=advance_from.value,
        advance_to=RunState.architect_running.value,
    )


def invoke_proposer_research(run: Run, arn: str, *, advance_from: RunState) -> InvokeAgent:
    """Dispatch the proposer for an issue-driven research run.

    The agent's research substrate is the issue body (which carries the
    URLs the user asked us to read). ``run.intent`` holds only the issue
    title — useful for a one-line preamble but not for fetching. We send
    ``title + body`` as the agent's ``intent`` so the URLs are visible
    to ``browse_url``-based extraction.
    """
    body = run.issue_body or ""
    title = run.intent or ""
    intent = f"{title}\n\n{body}".strip() if body else title
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-proposer",
        payload={
            "project_slug": run.project_slug,
            "target_repo": run.target_repo,
            "trigger_reason": "research",
            "intent": intent,
            "issue_number": run.issue_number,
            "triggering_comment_body": run.triggering_comment_body or "",
            "triggering_commenter": run.triggering_commenter or "",
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": run.actor_id,
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk="STATE",
        advance_from=advance_from.value,
        advance_to=RunState.proposer_running.value,
    )


# ---------------------------------------------------------------------------
# triage_decided → architect / proposer / cancel
# ---------------------------------------------------------------------------


_TRIAGE_CLOSE_LABELS: Mapping[str, tuple[str, str]] = {
    "defer": ("aidlc:deferred", "triage deferred"),
    "decline": ("aidlc:declined", "triage declined"),
}


def handle_triage_decided(run: Run) -> Action:
    """Branch on the triage agent's ``action`` decision.

    ``proceed`` → dispatch architect.
    ``research`` → dispatch proposer.
    ``ask`` → comment + label ``aidlc:awaiting-response``; cancel.
    ``defer`` / ``decline`` → label the issue and cancel.
    """
    action = run.triage_action or "proceed"
    if action == "proceed":
        return triage_proceed(run)
    if action == "research":
        return triage_research(run)
    if action == "ask":
        return triage_ask(run)
    if action in _TRIAGE_CLOSE_LABELS:
        label, reason = _TRIAGE_CLOSE_LABELS[action]
        return triage_close(run, label=label, reason=reason)
    return Noop(f"unknown triage action: {action}")


def triage_proceed(run: Run) -> Action:
    """Triage said ``proceed`` — dispatch the architect."""
    arn = runtime_arn("architect")
    if not arn:
        return Noop("architect runtime ARN not yet provisioned")
    return invoke_architect(run, arn, advance_from=RunState.triage_decided)


def triage_research(run: Run) -> Action:
    """Triage said ``research`` — dispatch the proposer."""
    arn = runtime_arn("proposer")
    if not arn:
        return Noop("proposer runtime ARN not yet provisioned")
    return invoke_proposer_research(run, arn, advance_from=RunState.triage_decided)


def triage_ask(run: Run) -> Action:
    """Post a clarifying comment + ``aidlc:awaiting-response`` label, then cancel."""
    if not run.target_repo or run.issue_number is None:
        return Noop("triage ask: missing target_repo / issue_number")
    actions: list[Action] = [
        InvokeRepoHelper(
            op="comment_issue",
            args={
                "repo": run.target_repo,
                "issue_number": run.issue_number,
                "body": (
                    "Triage needs more information before I can start. "
                    "Reply with the missing details and add `/aidlc go` to retry."
                ),
            },
        ),
        InvokeRepoHelper(
            op="label_issue",
            args={
                "repo": run.target_repo,
                "issue_number": run.issue_number,
                "labels": ["aidlc:awaiting-response"],
            },
        ),
        emit_run_cancel(run, source="comment_command", reason="triage asked for clarification"),
    ]
    return CompoundAction(actions=tuple(actions))


def triage_close(run: Run, *, label: str, reason: str) -> Action:
    """Label the issue (defer / decline), then cancel the run."""
    if not run.target_repo or run.issue_number is None:
        return Noop(f"{reason}: missing target_repo / issue_number")
    actions: list[Action] = [
        InvokeRepoHelper(
            op="label_issue",
            args={
                "repo": run.target_repo,
                "issue_number": run.issue_number,
                "labels": [label],
            },
        ),
        emit_run_cancel(run, source="comment_command", reason=reason),
    ]
    return CompoundAction(actions=tuple(actions))


def emit_run_cancel(run: Run, *, source: str, reason: str) -> EmitEvent:
    """Build a ``RUN.CANCEL_REQUESTED`` envelope so the projector cancels the run."""
    return EmitEvent(
        envelope=EventEnvelope[RunCancelRequested](
            event_id=new_event_id(),
            type="RUN.CANCEL_REQUESTED",
            run_id=RunId(run.run_id),
            correlation_id=CorrelationId(run.correlation_id),
            actor_id="state_router",
            payload=RunCancelRequested(
                project_slug=run.project_slug,
                requestor=run.requestor,
                source=source,  # ty: ignore[invalid-argument-type]
                reason=reason,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# designed → critic
# ---------------------------------------------------------------------------


def handle_designed(run: Run) -> Action:
    """Architect produced a plan — dispatch the critic against it.

    The critic reads ``plan.md`` from S3 and emits an adversarial
    review. Advisory only — its findings inform the implementer but do
    not gate the run.
    """
    if not run.plan_s3_key:
        return Noop("designed without plan_s3_key — projector hasn't projected yet")
    arn = runtime_arn("critic")
    if not arn:
        return Noop("critic runtime ARN not yet provisioned")
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-critic",
        payload={
            "project_slug": run.project_slug,
            "plan_s3_key": run.plan_s3_key,
            "intent": run.intent,
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": run.actor_id,
            "requestor_sub": run.requestor_sub,
            "target_repo": run.target_repo,
            "source_issue_url": run.source_issue_url,
            "source_issue_title": run.source_issue_title or run.issue_title,
            "source_issue_body": run.source_issue_body or run.issue_body,
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk="STATE",
        advance_from=RunState.designed.value,
        advance_to=RunState.critic_running.value,
    )


# ---------------------------------------------------------------------------
# critiqued → implementer (first pass)
# ---------------------------------------------------------------------------


def handle_critiqued(run: Run) -> Action:
    """Critic done — dispatch the implementer in ``mode=implementation``.

    The implementer reads both ``plan.md`` and ``critique.md`` from S3,
    works on a single branch ``aidlc/impl/{run_id}``, and opens the
    single impl PR for the whole run. It emits ``IMPL_PR.OPENED`` when
    done.
    """
    if not run.plan_s3_key:
        return Noop("critiqued without plan_s3_key — projector hasn't projected yet")
    arn = runtime_arn("implementer")
    if not arn:
        return Noop("implementer runtime ARN not yet provisioned")
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-impl",
        payload={
            "project_slug": run.project_slug,
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": "state_router",
            "mode": "implementation",
            "plan_s3_key": run.plan_s3_key,
            "critique_s3_key": run.critique_s3_key,
            "revision_number": 0,
            "requestor_sub": run.requestor_sub,
            "target_repo": run.target_repo,
            "source_issue_url": run.source_issue_url,
        },
        target_pk=f"RUN#{run.run_id}",
        target_sk="STATE",
        advance_from=RunState.critiqued.value,
        advance_to=RunState.implementer_running.value,
    )


# ---------------------------------------------------------------------------
# impl_pr_open → reviewer + tester + code_critic in parallel
# ---------------------------------------------------------------------------


def handle_impl_pr_open(run: Run) -> Action:
    """Implementer opened the PR — dispatch the three validators in parallel.

    All three target the unified impl PR (``run.pr_url``). Reviewer is
    the gatekeeper: its ``REVIEW.READY`` verdict drives the run's next
    transition. Tester and code-critic findings are advisory.

    Code-critic specifically reviews the implementation against the
    **original GitHub issue** (not just the architect's plan), so it
    additionally receives ``source_issue_url`` + title + body.

    The compound AdvanceState moves the run ``impl_pr_open →
    validation_running`` once all three invokes are queued; subsequent
    beacons in ``validation_running`` no-op.
    """
    if not run.pr_url:
        return Noop("impl_pr_open without pr_url — projector hasn't projected yet")
    if not run.plan_s3_key:
        return Noop("impl_pr_open without plan_s3_key — architect output missing")
    invokes: list[InvokeAgent] = []
    for agent_name in ("reviewer", "tester", "code_critic"):
        arn = runtime_arn(agent_name)
        if not arn:
            continue
        invokes.append(invoke_validator(run, agent_name, arn))
    if not invokes:
        return Noop("no validator runtimes provisioned")
    return CompoundAction(
        actions=(
            *invokes,
            AdvanceState(
                target_pk=f"RUN#{run.run_id}",
                target_sk="STATE",
                advance_from=RunState.impl_pr_open.value,
                advance_to=RunState.validation_running.value,
            ),
        ),
    )


def invoke_validator(run: Run, agent_name: str, arn: str) -> InvokeAgent:
    """Build the InvokeAgent for one validator targeting the impl PR."""
    payload: dict[str, Any] = {
        "project_slug": run.project_slug,
        "plan_s3_key": run.plan_s3_key,
        "pr_url": run.pr_url,
        "run_id": run.run_id,
        "correlation_id": run.correlation_id,
        "actor_id": "state_router",
        "requestor_sub": run.requestor_sub,
        "revision_number": run.revision_count,
    }
    if agent_name == "code_critic":
        payload["source_issue_url"] = run.source_issue_url
        payload["source_issue_title"] = run.source_issue_title or run.issue_title
        payload["source_issue_body"] = run.source_issue_body or run.issue_body
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-{agent_name}-r{run.revision_count}",
        payload=payload,
    )


# ---------------------------------------------------------------------------
# validation_complete → checks gate or revision dispatch
# ---------------------------------------------------------------------------


def handle_validation_complete(run: Run) -> Action:
    """Branch on reviewer verdict — merge gate or revision pass.

    ``approve`` / ``comment`` → read the impl PR's current Checks
    aggregate via ``repo_helper.get_check_state``:

    * ``passed`` → emit ``CHECKS.PASSED`` → ``awaiting_human_merge``.
    * ``failed`` → emit ``CHECKS.FAILED`` → ``revising`` (counts toward
      the revision cap).
    * ``pending`` → advance to ``awaiting_checks`` so the next
      ``CHECKS.PASSED`` / ``CHECKS.FAILED`` webhook lands the run on
      the right cursor.

    ``request_changes`` → dispatch the implementer in ``mode=revision``
    and advance to ``revising``. After ``MAX_REVISIONS`` automated
    cycles the run fails into the human's lap with ``RUN.FAILED`` so
    the loop can't spend tokens forever.
    """
    verdict = run.reviewer_verdict
    if verdict in {"approve", "comment", ""}:
        return handle_validation_approve(run)
    if verdict == "request_changes":
        return handle_validation_request_changes(run)
    return Noop(f"unknown reviewer verdict: {verdict!r}")


def handle_validation_approve(run: Run) -> Action:
    """Reviewer approved — gate on GitHub Checks state.

    ``run.check_state`` is the aggregate the projector wrote when the
    most recent ``CHECKS.PASSED`` / ``CHECKS.FAILED`` event landed. If
    nothing has projected yet, we proactively advance into
    ``awaiting_checks`` so the next webhook can land the run on the
    correct cursor without racing this beacon.
    """
    if run.check_state == "passed":
        return AdvanceState(
            target_pk=f"RUN#{run.run_id}",
            target_sk="STATE",
            advance_from=RunState.validation_complete.value,
            advance_to=RunState.awaiting_human_merge.value,
        )
    if run.check_state == "failed":
        return dispatch_revision(run, advance_from=RunState.validation_complete, automated=True)
    return AdvanceState(
        target_pk=f"RUN#{run.run_id}",
        target_sk="STATE",
        advance_from=RunState.validation_complete.value,
        advance_to=RunState.awaiting_checks.value,
    )


def handle_validation_request_changes(run: Run) -> Action:
    """Reviewer requested changes — dispatch revision or fail at cap."""
    if run.revision_count >= MAX_REVISIONS:
        return emit_run_failed(
            run,
            reason=(
                f"revision cap ({MAX_REVISIONS}) hit while reviewer.verdict "
                "is still request_changes"
            ),
        )
    return dispatch_revision(run, advance_from=RunState.validation_complete, automated=True)


# ---------------------------------------------------------------------------
# revising → implementer (mode=revision)
# ---------------------------------------------------------------------------


def handle_revising(run: Run) -> Action:
    """Dispatch the implementer to apply pending revision feedback.

    The projector writes ``pending_revision_feedback`` to the run row
    when the triggering event lands (CHECKS.FAILED, IMPL.ITERATION_REQUESTED,
    or a changes-requested REVIEW.READY). The implementer consumes it on
    revision dispatch.

    Note: ``handle_revising`` is reached only when the run is already
    in ``revising`` (the projector advanced it via transition). It does
    not increment ``revision_count`` itself — that's the dispatcher's
    job in :func:`dispatch_revision` (called from
    :func:`handle_validation_complete`). When ``revising`` is reached
    directly via CHECKS.FAILED / IMPL.ITERATION_REQUESTED projection,
    we still need to dispatch the implementer here.
    """
    return dispatch_revision(
        run,
        advance_from=None,
        automated=False,
        already_in_revising=True,
    )


def dispatch_revision(
    run: Run,
    *,
    advance_from: RunState | None,
    automated: bool,
    already_in_revising: bool = False,
) -> Action:
    """Build the implementer revision invoke + optional state advance.

    Args:
        run: parsed run state.
        advance_from: the state the run is currently in (the implementer
            invoke includes the conditional advance). ``None`` when the
            caller is already in ``revising`` and just needs to fire the
            implementer.
        automated: ``True`` when this revision is automated (validator-
            or CI-driven); ``False`` for human-mention revisions. Used
            to decide whether the new revision number counts toward the
            cap (automated ones do; human ones don't — but
            :func:`handle_validation_complete` checks the cap before
            calling here, and human-mention revisions never reach this
            via validation_complete).
        already_in_revising: ``True`` when the run is already in
            ``revising`` (called from :func:`handle_revising`) — skip
            the state advance.
    """
    arn = runtime_arn("implementer")
    if not arn:
        return Noop("implementer runtime ARN not yet provisioned")
    next_revision = run.revision_count + (1 if automated else 0)
    invoke = build_revision_invoke(run, arn, next_revision=next_revision)
    if already_in_revising or advance_from is None:
        return invoke
    return CompoundAction(
        actions=(
            invoke,
            AdvanceState(
                target_pk=f"RUN#{run.run_id}",
                target_sk="STATE",
                advance_from=advance_from.value,
                advance_to=RunState.revising.value,
            ),
        ),
    )


def build_revision_invoke(run: Run, arn: str, *, next_revision: int) -> InvokeAgent:
    """Construct the InvokeAgent for the implementer in ``mode=revision``."""
    return InvokeAgent(
        runtime_arn=arn,
        runtime_session_id=f"{run.run_id}-revision-{next_revision}",
        payload={
            "project_slug": run.project_slug,
            "run_id": run.run_id,
            "correlation_id": run.correlation_id,
            "actor_id": "state_router",
            "mode": "revision",
            "plan_s3_key": run.plan_s3_key,
            "critique_s3_key": run.critique_s3_key,
            "pr_url": run.pr_url,
            "revision_number": next_revision,
            "revision_feedback": list(run.pending_revision_feedback),
            "target_repo": run.target_repo,
            "source_issue_url": run.source_issue_url,
            "requestor_sub": run.requestor_sub,
        },
    )


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------


def emit_run_failed(run: Run, *, reason: str) -> EmitEvent:
    """Emit ``RUN.FAILED`` so the projector advances to ``failed``."""
    return EmitEvent(
        envelope=EventEnvelope[RunFailed](
            event_id=new_event_id(),
            type="RUN.FAILED",
            run_id=RunId(run.run_id),
            correlation_id=CorrelationId(run.correlation_id),
            actor_id="state_router",
            payload=RunFailed(
                project_slug=run.project_slug,
                failed_state=(run.current_state or RunState.failed).value,
                error_class="RevisionCapReached",
                error_message=reason,
                retryable=False,
            ),
        ),
    )


def noop_waiting(run: Run) -> Action:
    """No-op for states that wait on an external event."""
    return Noop(f"waiting in {run.current_state}")


def terminal(run: Run) -> Action:
    """Terminal state — beacon should be deleted by the handler."""
    return Noop(f"terminal: {run.current_state}")


RUN_DISPATCH: Mapping[RunState, RunHandler] = {
    RunState.received: handle_received,
    RunState.triaging: noop_waiting,
    RunState.triage_decided: handle_triage_decided,
    RunState.architect_running: noop_waiting,
    RunState.designed: handle_designed,
    RunState.critic_running: noop_waiting,
    RunState.critiqued: handle_critiqued,
    RunState.implementer_running: noop_waiting,
    RunState.impl_pr_open: handle_impl_pr_open,
    RunState.validation_running: noop_waiting,
    RunState.validation_complete: handle_validation_complete,
    RunState.revising: handle_revising,
    RunState.awaiting_checks: noop_waiting,
    RunState.awaiting_human_merge: noop_waiting,
    RunState.proposer_running: noop_waiting,
    RunState.done: terminal,
    RunState.failed: terminal,
    RunState.cancelled: terminal,
}
