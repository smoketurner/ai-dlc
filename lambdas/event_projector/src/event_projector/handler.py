"""Projector Lambda — fans out platform events into the read model + memory.

Triggered by every event on the platform EventBridge bus. Every event
produces exactly one ``TransactWriteItems`` containing:

* The EVENT timeline row (``sk=EVENT#{event_id}``) with
  ``attribute_not_exists(sk)`` — this row is the master idempotency
  key. A re-delivered envelope fails the condition and the entire
  transaction rolls back.
* The STATE row Update — always carries the metadata clauses (status,
  ``updated_at``, ``if_not_exists(project_slug)``, GSI keys for
  issue-driven runs, usage ``ADD`` clauses) and, when the event
  advances run-level state or accumulates revision feedback, the
  corresponding clauses in the same Update.
* The OUTBOX row Put — only when state advanced. The EventBridge Pipe
  forwards it to the state-router beacon queue.

Because the transaction is atomic, re-delivery is a complete no-op —
no double-counted usage totals, no duplicate memory writes, no
duplicate outbox rows. Race losses (two events targeting the same row
concurrently) drop cleanly via the conditional-check on the STATE
update.

After a successful commit, the projector forwards the envelope to
AgentCore Memory via ``CreateEvent``. Memory writes are gated on the
transaction succeeding, so they're also idempotent on event_id.

The platform now drives one PR per issue — there are no per-task DDB
rows. Architect/Critic produce internal S3 artifacts; the implementer
opens a single PR (``IMPL_PR.OPENED``); validators run once per
validation pass against that PR. Auto-revisions are triggered by
``CHECKS.FAILED`` and validator ``request_changes`` verdicts;
``IMPL.ITERATION_REQUESTED`` carries human ``@aidlc-bot`` mention
feedback (uncapped). All revision-driving feedback accumulates onto
``pending_revision_feedback`` on the STATE row; the state-router
consumes the queue when dispatching the implementer in
``mode=revision``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import cache
from typing import TYPE_CHECKING, Any, cast

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.parser import ValidationError, parse
from aws_lambda_powertools.utilities.parser.envelopes import EventBridgeEnvelope
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import BotoCoreError, ClientError

from common.ddb import (
    PutBuilder,
    TransactWriteItemsBuilder,
    UpdateBuilder,
    deserialize_item,
)
from common.events import EventType as PlatformEventType
from common.events import UntypedEnvelope
from common.state import RunState
from common.state_transitions import apply_run_transition

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = Logger(service="event_projector")
tracer = Tracer(service="event_projector")
metrics = Metrics(namespace="ai-dlc", service="event_projector")

OUTBOX_TTL_SECONDS = 3600
"""How long an outbox row lives before DDB TTL sweeps it.

The pipe forwards within seconds; the stream record persists for 24h
regardless of TTL, so a pipe outage of up to 24h still recovers. TTL
is purely table hygiene.
"""

DISPATCH_RESET_EVENTS = frozenset(
    {
        "DESIGN.READY",
        "CRITIQUE.READY",
        "IMPL_PR.OPENED",
        "REVISION.READY",
    },
)
"""Events that prove the prior dispatch reached the agent and ran.

Each one resets ``dispatch_failure_count`` to 0 on the STATE row the
projector advances. Advisor events (``REVIEW.READY``,
``TEST_REPORT.READY``, ``CODE_CRITIQUE.READY``) are not listed because
the dispatches that produce them are gated by an outer ``GuardedAdvance``
and don't increment the counter in the first place.
"""

REVISION_TRIGGER_BY_EVENT: dict[str, str] = {
    "CHECKS.FAILED": "ci_failure",
    "IMPL.ITERATION_REQUESTED": "human_mention",
}
"""Maps the events that advance a run into ``revising`` to the trigger
label ``state_router.handle_revising`` reads to decide whether the
revision cap applies. The reviewer-changes-requested path advances from
``validation_complete`` inside the state-router itself and stamps its
own label there."""

REVISION_FEEDBACK_ACCUMULATOR_STATES = frozenset(
    {
        RunState.impl_pr_open,
        RunState.validation_running,
        RunState.validation_complete,
        RunState.awaiting_checks,
        RunState.awaiting_human_merge,
        RunState.revising,
    },
)
"""Run states where revision feedback is appended to the queue.

The state-router consumes ``pending_revision_feedback`` when it
dispatches the implementer in ``mode=revision``. Late mentions that
arrive while the implementer is already iterating queue up onto the
same list; the next revision pass folds them in.
"""


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


@cache
def agentcore() -> Any:
    """Process-cached AgentCore data-plane client (memory CreateEvent)."""
    return boto3.client("bedrock-agentcore")


def runs_table() -> str:
    """DynamoDB runs table name."""
    return os.environ["AIDLC_RUNS_TABLE"]


def memory_id() -> str:
    """AgentCore Memory resource ID."""
    return os.environ["AIDLC_MEMORY_ID"]


@logger.inject_lambda_context(log_event=False)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Fan out one EventBridge event into the read model + memory + outbox."""
    if "detail" in event and "detail-type" in event:
        return handle_eventbridge(event)
    logger.warning("unknown trigger shape", extra={"keys": sorted(event.keys())})
    return {"ok": False, "error": "unknown trigger"}


def handle_eventbridge(event: dict[str, Any]) -> dict[str, Any]:
    """Single EventBridge invocation; ``event['detail']`` is the envelope."""
    try:
        envelope = cast(
            "UntypedEnvelope",
            parse(
                event=normalise(event),
                model=UntypedEnvelope,
                envelope=EventBridgeEnvelope,
            ),
        )
    except ValidationError as exc:
        logger.warning("invalid event", extra={"errors": exc.errors()})
        return {"ok": False, "error": "validation_error"}
    detail = envelope.model_dump(mode="json")
    run_id = str(envelope.run_id)
    event_type = envelope.type
    committed = project_event(envelope=envelope, detail=detail)
    if committed:
        forward_to_memory(detail)
    metrics.add_metric(name="EventsProjected", unit=MetricUnit.Count, value=1)
    return {"ok": True, "run_id": run_id, "type": event_type, "committed": committed}


def normalise(event: dict[str, Any]) -> dict[str, Any]:
    """Decode ``detail`` if EventBridge ships it as a JSON string."""
    detail = event.get("detail")
    if isinstance(detail, str):
        return {**event, "detail": json.loads(detail)}
    return event


# ---------------------------------------------------------------------------
# Projection: one TransactWriteItems per event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunMode:
    """How a run-level event affects the STATE row.

    ``from_state`` is what we read off the row before building the
    transaction; ``next_state`` is the target if the event advances
    state. ``accumulates_feedback`` is True when the event appends to
    ``pending_revision_feedback`` (``IMPL.ITERATION_REQUESTED`` or
    ``CHECKS.FAILED`` in eligible states), independent of whether it
    also advances state.
    """

    from_state: RunState | None
    next_state: RunState | None = None
    accumulates_feedback: bool = False


@tracer.capture_method
def project_event(*, envelope: UntypedEnvelope, detail: dict[str, Any]) -> bool:
    """Build and commit one ``TransactWriteItems`` for this event.

    Returns ``True`` when the transaction committed, ``False`` on a
    conditional-check loss (re-delivery via the EVENT row, or a race
    loss on the STATE condition). On ``False`` the caller skips the
    AgentCore Memory write so memory is also idempotent on event_id.
    """
    run_id = str(envelope.run_id)
    event_type = envelope.type or "UNKNOWN"
    transaction = TransactWriteItemsBuilder()
    transaction.put(event_row_item(run_id, event_type, detail))
    add_run_event_items(transaction, run_id, event_type, detail)
    committed = transaction.commit(ddb())
    if not committed:
        logger.info(
            "event already projected (idempotent no-op)",
            extra={"run_id": run_id, "event_type": event_type},
        )
    return committed


def add_run_event_items(
    transaction: TransactWriteItemsBuilder,
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
) -> None:
    """Add the STATE Update + optional OUTBOX Put for a run-level event."""
    current = read_state_attribute(
        pk=f"RUN#{run_id}",
        sk="STATE",
        attribute="current_state",
        enum_type=RunState,
        log_context={"run_id": run_id},
    )
    next_state = apply_run_transition(
        event_type=cast("PlatformEventType", event_type),
        current_state=current,
    )
    mode = build_run_mode(
        current=current,
        next_state=next_state,
        event_type=event_type,
    )
    transaction.update(run_state_item(run_id, event_type, detail, mode))
    if mode.next_state is not None:
        transaction.put(outbox_item(run_id, detail))


def build_run_mode(
    *,
    current: RunState | None,
    next_state: RunState | None,
    event_type: str,
) -> RunMode:
    """Pick the right ``RunMode`` for ``event_type`` given the current cursor.

    ``IMPL.ITERATION_REQUESTED`` and ``CHECKS.FAILED`` accumulate
    feedback onto ``pending_revision_feedback`` whenever the run is
    in an eligible state — whether or not they also advance the cursor.
    All other events use a plain advance-only mode.
    """
    accumulates = (
        event_type in {"IMPL.ITERATION_REQUESTED", "CHECKS.FAILED"}
        and current in REVISION_FEEDBACK_ACCUMULATOR_STATES
    )
    return RunMode(
        from_state=current,
        next_state=next_state,
        accumulates_feedback=accumulates,
    )


# ---------------------------------------------------------------------------
# Item builders
# ---------------------------------------------------------------------------


def event_row_item(
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
) -> PutBuilder:
    """The EVENT timeline row — master idempotency key for the event.

    A re-delivered envelope fails ``attribute_not_exists(sk)`` and the
    entire transaction rolls back, which is what makes downstream
    metadata + state writes safe under at-least-once delivery.
    """
    event_id = detail.get("event_id", "unknown")
    return PutBuilder(
        table=runs_table(),
        item={
            "pk": f"RUN#{run_id}",
            "sk": f"EVENT#{event_id}",
            "type": event_type,
            "envelope": json.dumps(detail),
        },
    ).condition_not_exists("sk")


def run_state_item(
    run_id: str,
    event_type: str,
    detail: dict[str, Any],
    mode: RunMode,
) -> UpdateBuilder:
    """Build the STATE row Update for this event.

    Composes always-on metadata (status, timestamps, last_event_*),
    payload projections (project_slug, GSI keys, pr_url, etc.), usage
    totals, optional state-advance, optional revision-feedback
    accumulator, and the right ``ConditionExpression`` onto a single
    ``UpdateBuilder``.

    Pure metadata-only updates carry no ConditionExpression — the
    EVENT row's ``attribute_not_exists(sk)`` already gates re-delivery.
    Advancing or accumulator events condition on the cursor's exact
    value (or ``attribute_not_exists(current_state)`` for the first
    event) so a concurrent event that just moved past drops cleanly
    via CCFE.
    """
    payload = detail.get("payload") or {}
    update = (
        UpdateBuilder(
            table=runs_table(),
            key={"pk": f"RUN#{run_id}", "sk": "STATE"},
        )
        .set("status", event_type)
        .set("updated_at", detail.get("timestamp", ""))
        .set("last_event_id", detail.get("event_id", ""))
        .set("last_event_at", detail.get("timestamp", ""))
    )
    apply_payload_projections(update, run_id=run_id, event_type=event_type, payload=payload)
    apply_usage_totals(update, payload=payload)
    apply_run_state_advance(update, mode=mode, event_type=event_type)
    apply_revision_feedback_accumulator(
        update,
        mode=mode,
        event_type=event_type,
        payload=payload,
    )
    apply_run_state_condition(update, mode=mode)
    return update


def apply_payload_projections(
    update: UpdateBuilder,
    *,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Project payload fields onto the STATE row.

    Covers the always-on fields (``project_slug`` via ``if_not_exists``,
    ``REQUEST.RECEIVED`` GSI keys + source issue fields) and the per-
    event-type fields (``ISSUE.TRIAGED`` triage_action +
    decision_s3_key, ``DESIGN.READY`` plan_s3_key, ``CRITIQUE.READY``
    + ``CODE_CRITIQUE.READY`` + ``REVIEW.READY`` severity counts,
    ``IMPL_PR.OPENED`` pr_url, ``CHECKS.*`` check_state, etc.). Each
    is gated on the payload field being well-formed.
    """
    project_slug = payload.get("project_slug")
    if isinstance(project_slug, str) and project_slug:
        update.set_if_not_exists("project_slug", project_slug)
    if event_type == "REQUEST.RECEIVED":
        apply_request_received_projections(update, run_id=run_id, payload=payload)
    apply_event_specific_projections(update, event_type=event_type, payload=payload)


def apply_request_received_projections(
    update: UpdateBuilder,
    *,
    run_id: str,
    payload: dict[str, Any],
) -> None:
    """Project ``REQUEST.RECEIVED`` source-issue fields + GSI keys."""
    source_issue_url = payload.get("source_issue_url")
    if isinstance(source_issue_url, str) and source_issue_url:
        update.set_if_not_exists("gsi1pk", f"ISSUE#{source_issue_url}")
        update.set_if_not_exists("gsi1sk", f"RUN#{run_id}")
        update.set_if_not_exists("source_issue_url", source_issue_url)
    source_issue_title = payload.get("source_issue_title")
    if isinstance(source_issue_title, str) and source_issue_title:
        update.set_if_not_exists("source_issue_title", source_issue_title)
    source_issue_body = payload.get("source_issue_body")
    if isinstance(source_issue_body, str) and source_issue_body:
        update.set_if_not_exists("source_issue_body", source_issue_body)


EVENT_PROJECTORS: dict[
    str,
    Callable[[UpdateBuilder, dict[str, Any]], None],
] = {}
"""Dispatch table from event_type → its STATE-row projection function.

Populated below once each projector is defined; keyed by event_type
string so :func:`apply_event_specific_projections` is a one-line
lookup instead of a tall if/elif chain.
"""


def apply_event_specific_projections(
    update: UpdateBuilder,
    *,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Project fields that exist only for specific event types.

    Looks ``event_type`` up in :data:`EVENT_PROJECTORS` and calls the
    matching focused projector. Unknown event types fall through with
    no per-event projection (the always-on metadata still lands).
    """
    projector = EVENT_PROJECTORS.get(event_type)
    if projector is not None:
        projector(update, payload)


def project_triage(update: UpdateBuilder, payload: dict[str, Any]) -> None:
    """Project the ``ISSUE.TRIAGED`` action + decision artefact onto STATE."""
    action = payload.get("action")
    if isinstance(action, str) and action:
        update.set("triage_action", action)
    decision_s3_key = payload.get("decision_s3_key")
    if isinstance(decision_s3_key, str) and decision_s3_key:
        update.set("decision_s3_key", decision_s3_key)


def project_design_ready(update: UpdateBuilder, payload: dict[str, Any]) -> None:
    """Project the architect's plan artefact onto STATE."""
    plan_s3_key = payload.get("plan_s3_key")
    if isinstance(plan_s3_key, str) and plan_s3_key:
        update.set("plan_s3_key", plan_s3_key)


def project_critique_ready(update: UpdateBuilder, payload: dict[str, Any]) -> None:
    """Project the critic's artefact + severity counts onto STATE."""
    critique_s3_key = payload.get("critique_s3_key")
    if isinstance(critique_s3_key, str) and critique_s3_key:
        update.set("critique_s3_key", critique_s3_key)
    apply_severity_counts(update, payload=payload, prefix="critique")


def project_impl_pr_opened(update: UpdateBuilder, payload: dict[str, Any]) -> None:
    """Project the impl PR URL + ``gsi_pr`` so webhooks can look it up."""
    pr_url = payload.get("pr_url")
    if isinstance(pr_url, str) and pr_url:
        update.set("pr_url", pr_url)
        update.set("gsi_pr", f"PR#{pr_url}")


def project_review_ready(update: UpdateBuilder, payload: dict[str, Any]) -> None:
    """Project the reviewer's verdict + severity counts onto STATE."""
    verdict = payload.get("verdict")
    if isinstance(verdict, str) and verdict:
        update.set("reviewer_verdict", verdict)
    apply_severity_counts(update, payload=payload, prefix="reviewer")


def project_test_report_ready(update: UpdateBuilder, payload: dict[str, Any]) -> None:
    """Project the tester's gap/suggested-test counters onto STATE."""
    gap_count = payload.get("gap_count")
    if isinstance(gap_count, int):
        update.set("tester_gap_count", gap_count)
    suggested = payload.get("suggested_test_count")
    if isinstance(suggested, int):
        update.set("suggested_test_count", suggested)


def project_code_critique_ready(update: UpdateBuilder, payload: dict[str, Any]) -> None:
    """Project the code-critic's artefact + severity counts onto STATE."""
    critique_s3_key = payload.get("critique_s3_key")
    if isinstance(critique_s3_key, str) and critique_s3_key:
        update.set("code_critic_critique_s3_key", critique_s3_key)
    apply_severity_counts(update, payload=payload, prefix="code_critic")


def project_revision_ready(update: UpdateBuilder, payload: dict[str, Any]) -> None:
    """Bump the revision counter on every ``REVISION.READY``.

    Prefers the ``revision_number`` carried on the payload (the
    authoritative counter the implementer reports); falls back to a
    DDB ``ADD 1`` for back-compat with payloads that omit it.
    """
    revision_number = payload.get("revision_number")
    if isinstance(revision_number, int) and revision_number >= 0:
        update.set("revision_count", revision_number)
    else:
        update.add("revision_count", 1)


def project_checks_passed(update: UpdateBuilder, payload: dict[str, Any]) -> None:
    """Project the green CI verdict + HEAD sha onto STATE."""
    update.set("check_state", "passed")
    head_sha = payload.get("head_sha")
    if isinstance(head_sha, str) and head_sha:
        update.set("check_head_sha", head_sha)


def project_checks_failed(update: UpdateBuilder, payload: dict[str, Any]) -> None:
    """Project the red CI verdict + HEAD sha onto STATE.

    The actual ``ci_failure`` :class:`FeedbackItem` is appended to
    ``pending_revision_feedback`` by
    :func:`apply_revision_feedback_accumulator` so the state-router
    can hand it to the implementer on the next revision dispatch.
    """
    update.set("check_state", "failed")
    head_sha = payload.get("head_sha")
    if isinstance(head_sha, str) and head_sha:
        update.set("check_head_sha", head_sha)


EVENT_PROJECTORS.update(
    {
        "ISSUE.TRIAGED": project_triage,
        "DESIGN.READY": project_design_ready,
        "CRITIQUE.READY": project_critique_ready,
        "IMPL_PR.OPENED": project_impl_pr_opened,
        "REVIEW.READY": project_review_ready,
        "TEST_REPORT.READY": project_test_report_ready,
        "CODE_CRITIQUE.READY": project_code_critique_ready,
        "REVISION.READY": project_revision_ready,
        "CHECKS.PASSED": project_checks_passed,
        "CHECKS.FAILED": project_checks_failed,
    },
)


def apply_severity_counts(
    update: UpdateBuilder,
    *,
    payload: dict[str, Any],
    prefix: str,
) -> None:
    """SET ``{prefix}_{high,medium,low}_severity_count`` from the payload."""
    for level in ("high", "medium", "low"):
        key = f"{level}_severity_count"
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            update.set(f"{prefix}_{level}_severity_count", value)


def apply_usage_totals(update: UpdateBuilder, *, payload: dict[str, Any]) -> None:
    """ADD per-event token / cost / duration totals when non-zero.

    Each ADD is conditional on the value being non-zero to avoid a
    no-op DDB write. Float ``cost_usd`` passes through the builder
    which Decimal-normalises before serialisation.
    """
    in_tokens = int(payload.get("token_in", 0) or 0)
    if in_tokens:
        update.add("total_token_in", in_tokens)
    out_tokens = int(payload.get("token_out", 0) or 0)
    if out_tokens:
        update.add("total_token_out", out_tokens)
    cost = float(payload.get("cost_usd", 0.0) or 0.0)
    if cost:
        update.add("total_cost_usd", cost)
    duration = int(payload.get("duration_ms", 0) or 0)
    if duration:
        update.add("total_duration_ms", duration)


def apply_run_state_advance(
    update: UpdateBuilder,
    *,
    mode: RunMode,
    event_type: str,
) -> None:
    """Advance ``current_state`` when ``mode.next_state`` is set.

    SET ``current_state``, ADD ``state_transitions``, and reset
    ``dispatch_failure_count`` to 0 for dispatch-completion events
    (the prior dispatch reached the agent and ran).

    When the transition lands the run in ``revising``, stamp
    ``last_revision_trigger`` so the state-router's ``handle_revising``
    knows whether the run came in via CI failure (automated, capped) or
    a human ``@aidlc-bot`` mention (uncapped).
    """
    if mode.next_state is None:
        return
    update.set("current_state", mode.next_state.value)
    update.add("state_transitions", 1)
    if event_type in DISPATCH_RESET_EVENTS:
        update.set("dispatch_failure_count", 0)
    if mode.next_state is RunState.revising:
        trigger = REVISION_TRIGGER_BY_EVENT.get(event_type)
        if trigger is not None:
            update.set("last_revision_trigger", trigger)


def apply_revision_feedback_accumulator(
    update: UpdateBuilder,
    *,
    mode: RunMode,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Append a ``FeedbackItem`` to ``pending_revision_feedback``.

    Fires when ``mode.accumulates_feedback`` is True. The feedback
    item shape matches :data:`common.runtime.FeedbackItem` — a
    discriminated union over ``ci_failure`` /
    ``issue_comment_mention`` / ``review_comment_mention`` /
    ``review_changes_requested``. Idempotency is enforced by adding
    ``delivery_id`` to the ``delivery_ids`` SS — webhooks that re-fire
    the same delivery hit the EVENT-row CCFE first, but the set is a
    second line of defence for downstream re-runs.
    """
    if not mode.accumulates_feedback:
        return
    item = build_feedback_item(event_type=event_type, payload=payload)
    if item is None:
        return
    update.list_append("pending_revision_feedback", [item])
    delivery_id = payload.get("delivery_id")
    if isinstance(delivery_id, str) and delivery_id:
        update.add("delivery_ids", {delivery_id})


def build_feedback_item(
    *,
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Translate the payload into a :data:`FeedbackItem`-shaped dict.

    Returns ``None`` when the payload doesn't carry the required
    fields — the projector then skips the accumulator but still
    advances state (the cursor change still matters even when the
    feedback is malformed).
    """
    if event_type == "CHECKS.FAILED":
        return build_ci_failure_feedback(payload=payload)
    if event_type == "IMPL.ITERATION_REQUESTED":
        return build_mention_feedback(payload=payload)
    return None


def build_ci_failure_feedback(*, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Build a ``ci_failure`` :data:`FeedbackItem` dict from a CHECKS.FAILED payload.

    The dashboard's webhook aggregates per-PR check_runs; we represent
    the failure compactly so the implementer can fetch the full
    workflow log on its own if needed. ``head_sha`` ties the failure
    to the PR's HEAD commit (so a re-push naturally invalidates it).
    """
    head_sha = payload.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        return None
    failed_count = payload.get("failed_workflow_count")
    summary = payload.get("summary") or ""
    workflow_name = (
        f"{failed_count} workflow(s) failed"
        if isinstance(failed_count, int) and failed_count > 0
        else "ci"
    )
    return {
        "kind": "ci_failure",
        "workflow_name": workflow_name,
        "conclusion": "failure",
        "head_sha": head_sha,
        "html_url": str(summary)[:512],
    }


def build_mention_feedback(*, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Build a mention :data:`FeedbackItem` dict from an IMPL.ITERATION_REQUESTED.

    The ``source`` discriminator picks the right :data:`FeedbackItem`
    variant — ``issue_comment_mention`` / ``review_comment_mention``
    / ``review_changes_requested``. Returns ``None`` (and the projector
    drops the feedback while still advancing state) when the id the
    discriminator requires is missing from the envelope.
    """
    body = payload.get("feedback_body")
    commenter = payload.get("commenter")
    if not isinstance(body, str) or not body.strip():
        return None
    if not isinstance(commenter, str) or not commenter:
        return None
    source = payload.get("source")
    builder = _MENTION_BUILDERS.get(source) if isinstance(source, str) else None
    return builder(payload=payload, body=body, commenter=commenter) if builder else None


def _issue_comment_feedback(
    *,
    payload: dict[str, Any],
    body: str,
    commenter: str,
) -> dict[str, Any] | None:
    """Build an ``issue_comment_mention`` feedback dict."""
    comment_id = payload.get("comment_id")
    if not isinstance(comment_id, int) or comment_id < 1:
        return None
    return {
        "kind": "issue_comment_mention",
        "comment_id": comment_id,
        "body": body,
        "commenter": commenter,
    }


def _review_comment_feedback(
    *,
    payload: dict[str, Any],
    body: str,
    commenter: str,
) -> dict[str, Any] | None:
    """Build a ``review_comment_mention`` feedback dict.

    ``path`` / ``commit_id`` are placeholders — the webhook layer
    carries the full review-comment context elsewhere if needed; the
    implementer's prompt only needs the body + commenter to act.
    """
    comment_id = payload.get("comment_id")
    if not isinstance(comment_id, int) or comment_id < 1:
        return None
    return {
        "kind": "review_comment_mention",
        "path": "(unknown)",
        "commit_id": "0" * 7,
        "comment_id": comment_id,
        "body": body,
        "commenter": commenter,
    }


def _review_changes_feedback(
    *,
    payload: dict[str, Any],
    body: str,
    commenter: str,
) -> dict[str, Any] | None:
    """Build a ``review_changes_requested`` feedback dict."""
    review_id = payload.get("review_id")
    if not isinstance(review_id, int) or review_id < 1:
        return None
    return {
        "kind": "review_changes_requested",
        "reviewer": commenter,
        "body": body,
        "review_id": review_id,
    }


_MENTION_BUILDERS: dict[
    str,
    Callable[..., dict[str, Any] | None],
] = {
    "issue_comment_mention": _issue_comment_feedback,
    "review_comment_mention": _review_comment_feedback,
    "review_changes_requested": _review_changes_feedback,
}


def apply_run_state_condition(update: UpdateBuilder, *, mode: RunMode) -> None:
    """Attach the STATE row's ConditionExpression.

    Pure metadata-only updates carry no condition — the EVENT row's
    ``attribute_not_exists(sk)`` already gates re-delivery. Advancing
    or accumulator events condition on the cursor's exact value (or
    ``attribute_not_exists(current_state)`` for the first event) so a
    concurrent event that just moved past drops cleanly via CCFE.
    """
    if mode.next_state is None and not mode.accumulates_feedback:
        return
    if mode.from_state is None:
        update.condition_not_exists("current_state")
    else:
        update.condition_eq("current_state", mode.from_state.value)


def outbox_item(run_id: str, detail: dict[str, Any]) -> PutBuilder:
    """The OUTBOX row the EventBridge Pipe forwards to the beacon queue."""
    event_id = detail.get("event_id", "")
    return PutBuilder(
        table=runs_table(),
        item={
            "pk": f"RUN#{run_id}",
            "sk": f"OUTBOX#{event_id}",
            "run_id": run_id,
            "project_slug": project_slug_from_envelope(detail=detail, run_id=run_id),
            "expire_at": int(datetime.now(UTC).timestamp()) + OUTBOX_TTL_SECONDS,
        },
    ).condition_not_exists("sk")


def project_slug_from_envelope(*, detail: dict[str, Any], run_id: str) -> str:
    """Read ``project_slug`` from the event payload; fall back to ``run_id``.

    The beacon queue is a Standard queue with SQS fair-queue grouping;
    the pipe sets ``MessageGroupId`` to the row's ``project_slug`` so
    noisy-neighbor metrics are reported per project. The fallback to
    ``run_id`` keeps the outbox write defensible if a future event
    omits ``project_slug``.
    """
    payload = detail.get("payload") or {}
    slug = payload.get("project_slug")
    if isinstance(slug, str) and slug:
        return slug
    return run_id


# ---------------------------------------------------------------------------
# State reads
# ---------------------------------------------------------------------------


def read_state_attribute[T: StrEnum](
    *,
    pk: str,
    sk: str,
    attribute: str,
    enum_type: type[T],
    log_context: dict[str, str],
) -> T | None:
    """Read one STR attribute off a runs-table row and parse it as a StrEnum.

    Returns ``None`` if the row is missing, the attribute is absent or
    empty, or the value can't be parsed by ``enum_type``. Aliases the
    attribute name via ``ExpressionAttributeNames`` so reserved-word
    attributes (``status``) work without per-call special-casing.
    """
    response = ddb().get_item(
        TableName=runs_table(),
        Key={"pk": {"S": pk}, "sk": {"S": sk}},
        ProjectionExpression="#a",
        ExpressionAttributeNames={"#a": attribute},
    )
    item = response.get("Item")
    if not item:
        return None
    decoded = deserialize_item(cast("dict[str, Any]", item))
    raw = decoded.get(attribute)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return enum_type(raw)
    except ValueError:
        logger.warning(
            "unknown attribute value in DDB",
            extra={"attribute": attribute, "raw": raw, **log_context},
        )
        return None


# ---------------------------------------------------------------------------
# AgentCore Memory pass-through
# ---------------------------------------------------------------------------


@tracer.capture_method
def forward_to_memory(envelope: dict[str, Any]) -> None:
    """Emit the envelope to AgentCore Memory as a CreateEvent.

    Only invoked after the projector's transaction commits, so memory
    writes are idempotent on event_id (a re-delivery rolls back the
    transaction and the projector returns early before reaching here).

    AgentCore Memory's ``CreateEvent`` requires ``eventTimestamp`` and
    a ``payload`` whose entries are a tagged union of ``conversational``
    or ``blob``. ``blob`` is a JSON-compatible Document, not raw bytes
    — we pass the envelope dict directly.
    """
    actor_id = envelope.get("payload", {}).get("project_slug") or envelope.get("actor_id", "system")
    session_id = envelope.get("run_id", "system")
    try:
        agentcore().create_event(
            memoryId=memory_id(),
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=parse_event_timestamp(envelope),
            payload=[{"blob": envelope}],
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning("memory CreateEvent failed", extra={"err": repr(exc)})
        metrics.add_metric(name="MemoryWriteFailures", unit=MetricUnit.Count, value=1)


def parse_event_timestamp(envelope: dict[str, Any]) -> datetime:
    """Parse the envelope's ISO-8601 ``timestamp`` field for boto3."""
    raw = envelope.get("timestamp")
    if isinstance(raw, str):
        return datetime.fromisoformat(raw)
    return datetime.now(UTC)
