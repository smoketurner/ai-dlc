"""Pure extractors — pull metadata fields out of a run's event history.

Every extractor is a pure function over ``Sequence[EnvelopeLike]``
with predictable defaults so missing data degrades to ``None`` /
empty string / 0 rather than raising — the executor decides what
to do when a value it needs isn't available yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EnvelopeLike(Protocol):
    """Structural subtype that covers both :class:`EventEnvelope` and :class:`UntypedEnvelope`.

    The router operates on whichever shape the handler produced. Typed
    envelopes have pydantic payloads; the untyped variant has a plain
    ``dict[str, Any]`` — :func:`get` smooths over that difference.

    Field types are deliberately :class:`Any` so both the literal-typed
    :class:`~common.events.EventEnvelope` and the dict-typed
    :class:`~common.events.UntypedEnvelope` satisfy the protocol, and
    so test stubs can use plain :class:`str` values.
    """

    type: Any
    event_id: Any
    run_id: Any
    correlation_id: Any
    payload: Any


def get(envelope: EnvelopeLike, field: str, default: Any = None) -> Any:
    """Read ``field`` off an envelope payload (dict or pydantic) with a default."""
    payload = envelope.payload
    if isinstance(payload, dict):
        return payload.get(field, default)
    return getattr(payload, field, default)


def first_payload_field(
    events: Sequence[EnvelopeLike],
    field: str,
    *,
    default: Any = None,
) -> Any:
    """Return the first non-empty value of ``field`` across payloads."""
    for event in events:
        value = get(event, field)
        if value:
            return value
    return default


def run_id(events: Sequence[EnvelopeLike]) -> str:
    """Run id is invariant across events; pull it from any."""
    if not events:
        return ""
    return str(events[0].run_id)


def correlation_id(events: Sequence[EnvelopeLike]) -> str:
    """Correlation id threads through every event for a run."""
    if not events:
        return ""
    return str(events[0].correlation_id)


def project_slug(events: Sequence[EnvelopeLike]) -> str:
    """Project slug lives on every payload that touches the run; pick the first."""
    return first_payload_field(events, "project_slug", default="")


def target_repo(events: Sequence[EnvelopeLike]) -> str:
    """GitHub ``owner/name`` carried on REQUEST.RECEIVED (and many others)."""
    return first_payload_field(events, "target_repo", default="")


def requestor(events: Sequence[EnvelopeLike]) -> str:
    """Human-readable submitter identity from REQUEST.RECEIVED."""
    return first_payload_field(events, "requestor", default="system")


def requestor_sub(events: Sequence[EnvelopeLike]) -> str | None:
    """Stable Cognito subject — used to fetch the user's GitHub OAuth token."""
    return first_payload_field(events, "requestor_sub")


def intent(events: Sequence[EnvelopeLike]) -> str:
    """Free-form description of the user's ask."""
    return first_payload_field(events, "intent", default="")


def source_issue_url(events: Sequence[EnvelopeLike]) -> str | None:
    """The GitHub issue that triggered the run, if any."""
    return first_payload_field(events, "source_issue_url")


def issue_payload(events: Sequence[EnvelopeLike]) -> dict[str, Any]:
    """Extract issue metadata for triage/research dispatch payloads."""
    triaged = next((e for e in events if e.type == "ISSUE.TRIAGED"), None)
    if triaged is not None:
        return {
            "issue_url": get(triaged, "issue_url", ""),
            "issue_number": get(triaged, "issue_number"),
            "issue_title": get(triaged, "issue_title", ""),
            "issue_body": get(triaged, "issue_body", ""),
            "issue_labels": list(get(triaged, "issue_labels", []) or []),
        }
    request = next((e for e in events if e.type == "REQUEST.RECEIVED"), None)
    if request is None:
        return {}
    return {
        "issue_url": get(request, "source_issue_url", ""),
        "issue_number": get(request, "issue_number"),
        "issue_title": get(request, "issue_title", ""),
        "issue_body": get(request, "issue_body", ""),
        "issue_labels": list(get(request, "issue_labels", []) or []),
    }


def plan_s3_key(events: Sequence[EnvelopeLike]) -> str:
    """Architect's plan.md S3 key, set on DESIGN.READY."""
    design = next((e for e in events if e.type == "DESIGN.READY"), None)
    return get(design, "plan_s3_key", "") if design else ""


def pr_url(events: Sequence[EnvelopeLike]) -> str:
    """The impl PR URL, set on IMPL_PR.OPENED and carried by later events."""
    opened = next((e for e in events if e.type == "IMPL_PR.OPENED"), None)
    if opened is not None:
        return get(opened, "pr_url", "")
    # Fall back to any later payload that carries pr_url
    return first_payload_field(events, "pr_url", default="")


def latest_triggering_comment(events: Sequence[EnvelopeLike]) -> tuple[str, str]:
    """Most recent comment body + commenter — used for triage context.

    Returns ``("", "")`` when no commenter signal is in the event log.
    """
    for event in reversed(events):
        body = get(event, "feedback_body") or get(event, "triggering_comment_body")
        commenter = get(event, "commenter") or get(event, "triggering_commenter")
        if body or commenter:
            return (str(body or ""), str(commenter or ""))
    return ("", "")


def revision_feedback(
    events: Sequence[EnvelopeLike],
) -> tuple[dict[str, Any], ...]:
    """Build the implementer's revision-feedback list from events since last revision.

    Walks from the most recent ``REVISION.READY`` (or ``IMPL_PR.OPENED``
    when no revisions have completed yet) forward and accumulates every
    revision-driving signal in that window:

    * ``IMPL.ITERATION_REQUESTED`` → ``issue_comment_mention`` /
      ``review_comment_mention`` / ``review_changes_requested`` based on
      the envelope's ``source``.
    * ``CHECKS.FAILED`` → ``ci_failure``.

    The implementer's ``ImplementerInput.revision_feedback`` accepts
    each of these as discriminated union variants.
    """
    boundary = -1
    for index, event in enumerate(events):
        if event.type in ("REVISION.READY", "IMPL_PR.OPENED"):
            boundary = index
    feedback: list[dict[str, Any]] = []
    for event in events[boundary + 1 :]:
        item = feedback_item_for(event)
        if item is not None:
            feedback.append(item)
    return tuple(feedback)


def feedback_item_for(event: EnvelopeLike) -> dict[str, Any] | None:
    """Build one feedback item from a single event, or ``None`` if not applicable."""
    if event.type == "IMPL.ITERATION_REQUESTED":
        return iteration_feedback(event)
    if event.type == "CHECKS.FAILED":
        return ci_failure_feedback(event)
    return None


def iteration_feedback(event: EnvelopeLike) -> dict[str, Any] | None:
    """Map an ``IMPL.ITERATION_REQUESTED`` envelope to a FeedbackItem dict."""
    body = get(event, "feedback_body", "")
    commenter = get(event, "commenter", "")
    if not isinstance(body, str) or not body.strip():
        return None
    if not isinstance(commenter, str) or not commenter:
        return None
    builder = _ITERATION_BUILDERS.get(get(event, "source", ""))
    return builder(event, body, commenter) if builder else None


def _issue_comment_mention(
    event: EnvelopeLike,
    body: str,
    commenter: str,
) -> dict[str, Any] | None:
    """Build an ``issue_comment_mention`` FeedbackItem, or ``None`` if id missing."""
    comment_id = get(event, "comment_id")
    if not isinstance(comment_id, int) or comment_id < 1:
        return None
    return {
        "kind": "issue_comment_mention",
        "comment_id": comment_id,
        "body": body,
        "commenter": commenter,
    }


def _review_comment_mention(
    event: EnvelopeLike,
    body: str,
    commenter: str,
) -> dict[str, Any] | None:
    """Build a ``review_comment_mention`` FeedbackItem, or ``None`` if id missing."""
    comment_id = get(event, "comment_id")
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


def _review_changes_requested(
    event: EnvelopeLike,
    body: str,
    commenter: str,
) -> dict[str, Any] | None:
    """Build a ``review_changes_requested`` FeedbackItem, or ``None`` if id missing."""
    review_id = get(event, "review_id")
    if not isinstance(review_id, int) or review_id < 1:
        return None
    return {
        "kind": "review_changes_requested",
        "reviewer": commenter,
        "body": body,
        "review_id": review_id,
    }


_ITERATION_BUILDERS: dict[
    str,
    Any,
] = {
    "issue_comment_mention": _issue_comment_mention,
    "review_comment_mention": _review_comment_mention,
    "review_changes_requested": _review_changes_requested,
}


def ci_failure_feedback(event: EnvelopeLike) -> dict[str, Any]:
    """Map a ``CHECKS.FAILED`` envelope to a ``ci_failure`` FeedbackItem dict."""
    return {
        "kind": "ci_failure",
        "workflow_name": "ci",
        "conclusion": "failure",
        "head_sha": get(event, "head_sha", ""),
        "html_url": get(event, "pr_url", ""),
    }
