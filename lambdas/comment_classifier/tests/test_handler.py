"""Tests for the comment classifier Lambda."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import boto3
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from moto import mock_aws
from pydantic import ValidationError

from comment_classifier import handler as h
from comment_classifier.handler import (
    ClassificationRequest,
    handler,
    parse_category,
    pr_slug,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mypy_boto3_s3.client import S3Client


def lambda_context() -> LambdaContext:
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="comment_classifier-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:t",
            aws_request_id="rid-1",
        ),
    )


@pytest.fixture
def s3_bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Client]:
    name = "ai-dlc-artifacts"
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", name)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    h.s3.cache_clear()  # invalidate the @cache so moto wraps the new client
    h.bedrock.cache_clear()
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=name)
        yield client


@pytest.fixture
def stub_bedrock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the cached Bedrock client with a MagicMock."""
    fake = MagicMock()
    monkeypatch.setattr(h, "bedrock", lambda: fake)
    return fake


def request_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "pr_url": "https://github.com/owner/name/pull/42",
        "comment_id": 1234,
        "author": "alice",
        "is_bot": False,
        "comment_body": "This loop reads the whole table into memory.",
    }
    base.update(overrides)
    return base


def bedrock_response(text: str) -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            },
        },
    }


def test_classification_request_validates() -> None:
    req = ClassificationRequest.model_validate(request_payload())
    assert req.pr_url.startswith("https://github.com/")
    assert req.is_bot is False


def test_classification_request_rejects_empty_body() -> None:
    with pytest.raises(ValidationError):
        ClassificationRequest.model_validate(request_payload(comment_body=""))


def test_pr_slug_handles_github_url() -> None:
    assert pr_slug("https://github.com/owner/name/pull/42") == "github-com-owner-name-pull-42"


def test_pr_slug_strips_special_chars() -> None:
    out = pr_slug("https://github.com/foo_bar/baz.qux/pull/1")
    assert "_" not in out
    assert "." not in out


def test_parse_category_extracts_valid_label() -> None:
    assert parse_category('{"category": "design"}') == "design"


def test_parse_category_handles_markdown_fences() -> None:
    text = '```json\n{"category": "bug"}\n```'
    assert parse_category(text) == "bug"


def test_parse_category_unknown_label_falls_back() -> None:
    assert parse_category('{"category": "blocker"}') == "unclear"


def test_parse_category_no_match_falls_back() -> None:
    assert parse_category("the comment looks fine to me") == "unclear"


def test_handler_writes_classified_comment_to_s3(
    s3_bucket: S3Client,
    stub_bedrock: MagicMock,
) -> None:
    stub_bedrock.converse.return_value = bedrock_response('{"category": "design"}')
    out = handler(request_payload(), lambda_context())
    assert out["ok"]
    assert out["category"] == "design"

    objs = s3_bucket.list_objects_v2(Bucket="ai-dlc-artifacts").get("Contents", [])
    assert len(objs) == 1
    body = s3_bucket.get_object(Bucket="ai-dlc-artifacts", Key=objs[0]["Key"])["Body"].read()
    record = json.loads(body)
    assert record["category"] == "design"
    assert record["comment_id"] == 1234
    assert record["author"] == "alice"
    assert record["classifier_model_id"].startswith("us.anthropic.")


def test_handler_falls_back_to_unclear_on_bedrock_error(
    s3_bucket: S3Client,
    stub_bedrock: MagicMock,
) -> None:
    stub_bedrock.converse.side_effect = RuntimeError("transient")
    out = handler(request_payload(), lambda_context())
    assert out["ok"]
    assert out["category"] == "unclear"


def test_handler_rejects_invalid_event(
    s3_bucket: S3Client,
    stub_bedrock: MagicMock,
) -> None:
    out = handler({"bogus": "yes"}, lambda_context())
    assert out["ok"] is False
    stub_bedrock.converse.assert_not_called()
