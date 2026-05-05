"""PR telemetry Lambda — capture GitHub PR webhook events.

Subscribed to four webhook event types:

  * ``pull_request`` (opened, closed, ready_for_review)
  * ``pull_request_review`` (submitted)
  * ``pull_request_review_comment`` (created)
  * ``issue_comment`` (created — only when on a PR)

For each event, looks up (or initialises) the PR's row in the
PR-telemetry DynamoDB table and updates the relevant counters /
timestamps. Only PRs the platform itself opened are tracked — the
hook recognises ours by the ``_run_id: <uuid>_`` marker in the PR
body footer that the Implementer always writes.

The Lambda is intentionally narrow — it does not classify comments
(that's the comment_classifier Lambda) and does not aggregate
metrics (the eval_aggregator schedule does). Its job is just to
land raw lifecycle facts in DynamoDB.
"""

from __future__ import annotations

import os
import re
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

logger = Logger(service="pr_telemetry")

RUN_ID_MARKER = re.compile(r"_run_id:\s*([0-9a-f-]{36})_", re.IGNORECASE)


@cache
def ddb() -> DynamoDBClient:
    """Process-cached DynamoDB client."""
    return boto3.client("dynamodb")


def telemetry_table() -> str:
    """DynamoDB table holding one row per platform-opened PR."""
    return os.environ["AIDLC_PR_TELEMETRY_TABLE"]


@logger.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Dispatch one GitHub webhook payload to the matching telemetry op."""
    action = str(event.get("action", ""))
    if "review" in event and "pull_request" in event:
        return handle_pull_request_review(event, action=action)
    if "comment" in event and ("issue" in event or "pull_request" in event):
        return handle_pull_request_comment(event, action=action)
    if "pull_request" in event:
        return handle_pull_request(event, action=action)
    logger.warning("unrecognised webhook shape", extra={"keys": sorted(event.keys())})
    return {"ok": False, "error": "unknown_webhook_shape"}


def handle_pull_request(event: dict[str, Any], *, action: str) -> dict[str, Any]:
    """``pull_request`` event handler — opened / closed / ready_for_review."""
    pr = event.get("pull_request", {})
    pr_url = str(pr.get("html_url") or "")
    body = str(pr.get("body") or "")
    run_id = parse_run_id(body)
    if run_id is None:
        logger.info("ignoring third-party PR", extra={"pr_url": pr_url})
        return {"ok": True, "ignored": "no_run_id_marker"}

    if action == "opened":
        record_pr_opened(pr_url=pr_url, pr=pr, run_id=run_id)
        return {"ok": True, "action": "opened", "pr_url": pr_url}
    if action == "closed":
        record_pr_closed(pr_url=pr_url, pr=pr)
        return {"ok": True, "action": "closed", "pr_url": pr_url}
    if action == "ready_for_review":
        record_marked_ready(pr_url=pr_url, pr=pr)
        return {"ok": True, "action": "ready_for_review", "pr_url": pr_url}
    return {"ok": True, "action": action, "ignored": "unwatched_action"}


def handle_pull_request_review(event: dict[str, Any], *, action: str) -> dict[str, Any]:
    """``pull_request_review`` event handler — count requested-changes cycles."""
    if action != "submitted":
        return {"ok": True, "action": action, "ignored": "unwatched_action"}
    review = event.get("review", {})
    pr = event.get("pull_request", {})
    pr_url = str(pr.get("html_url") or "")
    state = str(review.get("state") or "")
    if state == "changes_requested":
        increment_counter(pr_url=pr_url, attr="requested_changes_count")
    increment_counter(pr_url=pr_url, attr="review_count")
    return {"ok": True, "action": "submitted", "state": state, "pr_url": pr_url}


def handle_pull_request_comment(event: dict[str, Any], *, action: str) -> dict[str, Any]:
    """``pull_request_review_comment`` / ``issue_comment`` (on PR) handler."""
    if action != "created":
        return {"ok": True, "action": action, "ignored": "unwatched_action"}
    comment = event.get("comment", {})
    issue = event.get("issue", {})
    pr_block = comment.get("pull_request") or issue.get("pull_request") or {}
    pr_url = str(pr_block.get("html_url") or comment.get("html_url") or "")
    is_bot = bool(comment.get("user", {}).get("type") == "Bot")
    attr = "comment_count_bot" if is_bot else "comment_count_human"
    increment_counter(pr_url=pr_url, attr=attr)
    return {"ok": True, "pr_url": pr_url, "is_bot": is_bot}


def parse_run_id(body: str) -> str | None:
    """Extract the run id the Implementer writes in the PR-body footer."""
    match = RUN_ID_MARKER.search(body)
    if match is None:
        return None
    return match.group(1)


def record_pr_opened(*, pr_url: str, pr: dict[str, Any], run_id: str) -> None:
    """Insert the initial PR telemetry row when our PR is first opened."""
    item = {
        "pk": {"S": f"PR#{pr_url}"},
        "sk": {"S": "STATE"},
        "pr_url": {"S": pr_url},
        "run_id": {"S": run_id},
        "opened_at": {"S": str(pr.get("created_at") or "")},
        "opened_as_draft": {"BOOL": bool(pr.get("draft", False))},
        "merged": {"BOOL": False},
        "requested_changes_count": {"N": "0"},
        "review_count": {"N": "0"},
        "comment_count_human": {"N": "0"},
        "comment_count_bot": {"N": "0"},
    }
    ddb().put_item(
        TableName=telemetry_table(),
        Item=item,
        ConditionExpression="attribute_not_exists(sk)",
    )


def record_pr_closed(*, pr_url: str, pr: dict[str, Any]) -> None:
    """Update the row when the PR closes — merged or abandoned."""
    merged = bool(pr.get("merged", False))
    expr_parts = ["closed_at = :ts", "merged = :m"]
    values: dict[str, Any] = {
        ":ts": {"S": str(pr.get("closed_at") or "")},
        ":m": {"BOOL": merged},
    }
    if merged:
        expr_parts.append("merged_at = :ma")
        values[":ma"] = {"S": str(pr.get("merged_at") or "")}
    ddb().update_item(
        TableName=telemetry_table(),
        Key={"pk": {"S": f"PR#{pr_url}"}, "sk": {"S": "STATE"}},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeValues=values,
    )


def record_marked_ready(*, pr_url: str, pr: dict[str, Any]) -> None:
    """The maintainer flipped the PR from draft to Ready for review."""
    # Falls back to the PR author; the real ready_for_review webhook
    # carries the actor on `sender.login`, which we'd plumb through later.
    sender = pr.get("user", {}).get("login")
    expr_parts = ["marked_ready_at = :ts"]
    values: dict[str, Any] = {":ts": {"S": str(pr.get("updated_at") or "")}}
    if sender:
        expr_parts.append("marked_ready_by = :by")
        values[":by"] = {"S": str(sender)}
    ddb().update_item(
        TableName=telemetry_table(),
        Key={"pk": {"S": f"PR#{pr_url}"}, "sk": {"S": "STATE"}},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeValues=values,
    )


def increment_counter(*, pr_url: str, attr: str) -> None:
    """Atomically increment one of the count attributes on the PR row."""
    ddb().update_item(
        TableName=telemetry_table(),
        Key={"pk": {"S": f"PR#{pr_url}"}, "sk": {"S": "STATE"}},
        UpdateExpression="ADD #a :one",
        ExpressionAttributeNames={"#a": attr},
        ExpressionAttributeValues={":one": {"N": "1"}},
    )
