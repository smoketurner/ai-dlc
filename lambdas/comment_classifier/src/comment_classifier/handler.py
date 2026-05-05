"""Comment-classifier Lambda — category for one PR review comment.

Invocation paths:

* Direct invoke from ``pr_telemetry`` after a comment is captured —
  passes ``ClassificationRequest`` as the event payload.
* EventBridge schedule (catch-up) over the previous N hours of
  ``PR.COMMENT_CREATED`` events that haven't been classified yet.

Output: a :class:`common.eval.ClassifiedComment` written to
``s3://{artifacts_bucket}/evals/classified_comments/{date}/{pr_slug}/{comment_id}.json``
and returned in the response so the eval aggregator can stream it.

Bedrock failures and unparseable responses fall back to ``unclear``.
The classifier is advisory: a wrong label is recoverable on the next
quarterly hand-label calibration cycle (commitment C2).
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Annotated, Any, cast

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from comment_classifier.prompts import SYSTEM_PROMPT
from common.eval import ClassifiedComment, CommentCategory

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

logger = Logger(service="comment_classifier")

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
VALID_CATEGORIES: frozenset[str] = frozenset(
    {
        "nit",
        "bug",
        "design",
        "missing_test",
        "security",
        "performance",
        "documentation",
        "convention",
        "scope",
        "unclear",
    },
)


class ClassificationRequest(BaseModel):
    """Direct-invoke payload — one comment to classify."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    pr_url: Annotated[str, Field(min_length=1, max_length=512)]
    comment_id: Annotated[int, Field(ge=1)]
    author: Annotated[str, Field(min_length=1, max_length=128)]
    is_bot: bool
    comment_body: Annotated[str, Field(min_length=1, max_length=8192)]


@cache
def s3() -> S3Client:
    """Process-cached S3 client."""
    return boto3.client("s3")


@cache
def bedrock() -> Any:
    """Process-cached Bedrock runtime client."""
    return boto3.client("bedrock-runtime", region_name=os.environ["AWS_REGION"])


def artifacts_bucket() -> str:
    """Bucket holding classified comments under ``evals/classified_comments/``."""
    return os.environ["AIDLC_ARTIFACTS_BUCKET"]


def model_id() -> str:
    """Bedrock model id, overridable via ``AIDLC_BEDROCK_MODEL_ID``."""
    return os.environ.get("AIDLC_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)


@logger.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], _context: LambdaContext) -> dict[str, Any]:
    """Classify one PR review comment; persist + return the result."""
    try:
        req = ClassificationRequest.model_validate(event)
    except ValidationError as exc:
        logger.warning("invalid input", extra={"errors": json.loads(exc.json())})
        return {"ok": False, "error": "validation_error"}

    category = classify(req.comment_body)
    record = ClassifiedComment(
        pr_url=req.pr_url,
        comment_id=req.comment_id,
        author=req.author,
        is_bot=req.is_bot,
        category=category,
        quoted=req.comment_body[:2048],
        classified_at=datetime.now(tz=UTC),
        classifier_model_id=model_id(),
    )
    key = persist(record)
    logger.info(
        "comment classified",
        extra={
            "pr_url": req.pr_url,
            "comment_id": req.comment_id,
            "category": category,
            "s3_key": key,
        },
    )
    return {
        "ok": True,
        "category": category,
        "s3_key": key,
        "is_bot": req.is_bot,
    }


def classify(comment_body: str) -> CommentCategory:
    """Ask Bedrock Haiku for a category. Falls back to ``unclear`` on error."""
    try:
        response = bedrock().converse(
            modelId=model_id(),
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": comment_body}]}],
            inferenceConfig={"temperature": 0.0, "maxTokens": 64},
        )
    except Exception as exc:
        logger.warning("bedrock call failed; defaulting to unclear", extra={"err": repr(exc)})
        return "unclear"
    return parse_category(extract_text(response))


def extract_text(response: dict[str, Any]) -> str:
    """Pull the assistant text out of a Bedrock Converse response."""
    output = response.get("output", {})
    message = output.get("message", {})
    contents = message.get("content", [])
    parts = [c.get("text", "") for c in contents if "text" in c]
    return "".join(parts).strip()


_CATEGORY_RE = re.compile(r'"category"\s*:\s*"([a-z_]+)"')


def parse_category(text: str) -> CommentCategory:
    """Extract the ``category`` value; default to ``unclear`` on any miss."""
    match = _CATEGORY_RE.search(text)
    if match is None:
        logger.warning("no category in model output", extra={"text": text[:200]})
        return "unclear"
    label = match.group(1)
    if label not in VALID_CATEGORIES:
        logger.warning("unknown category from model", extra={"label": label})
        return "unclear"
    return cast("CommentCategory", label)


_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def pr_slug(pr_url: str) -> str:
    """``https://github.com/owner/name/pull/42`` → ``owner-name-pull-42``."""
    after_host = pr_url.split("//", 1)[-1]
    return _SLUG_RE.sub("-", after_host).strip("-")[:128]


def persist(record: ClassifiedComment) -> str:
    """Write the classified comment as JSON to S3; return the key."""
    date = record.classified_at.strftime("%Y-%m-%d")
    slug = pr_slug(record.pr_url)
    key = f"evals/classified_comments/{date}/{slug}/{record.comment_id}.json"
    s3().put_object(
        Bucket=artifacts_bucket(),
        Key=key,
        Body=record.model_dump_json().encode("utf-8"),
        ContentType="application/json",
    )
    return key
