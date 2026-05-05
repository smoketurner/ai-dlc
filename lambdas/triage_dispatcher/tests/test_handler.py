"""Unit tests for the Triage dispatcher Lambda."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import boto3
import pytest
from aws_lambda_powertools.utilities.typing import LambdaContext
from moto import mock_aws
from pydantic import ValidationError
from triage_dispatcher.bedrock import extract_json, extract_text
from triage_dispatcher.handler import (
    DECLINED_LABEL,
    DEFERRED_LABEL,
    IN_PROGRESS_LABEL,
)
from triage_dispatcher.models import TriageRequest, TriageVerdict
from triage_dispatcher.prompts import render_user_message

from triage_dispatcher import handler as h

BUS = "test-bus"
REPO_HELPER = "test-repo-helper"


def ctx() -> LambdaContext:
    """Minimal LambdaContext stand-in."""
    return cast(
        "LambdaContext",
        SimpleNamespace(
            function_name="triage_dispatcher-test",
            memory_limit_in_mb=128,
            invoked_function_arn="arn:aws:lambda:us-east-1:0:function:t",
            aws_request_id="rid-1",
        ),
    )


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Spin up moto for EventBridge + a stubbed Lambda invoke for repo_helper."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AIDLC_BUS_NAME", BUS)
    monkeypatch.setenv("AIDLC_REPO_HELPER_FUNCTION_NAME", REPO_HELPER)
    monkeypatch.setenv("AIDLC_BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    _safe_cache_clear(h.events_client)
    _safe_cache_clear(h.lambda_client)
    with mock_aws():
        boto3.client("events").create_event_bus(Name=BUS)
        yield
    _safe_cache_clear(h.events_client)
    _safe_cache_clear(h.lambda_client)


def _safe_cache_clear(fn: Any) -> None:
    """Call ``cache_clear`` only if the symbol still wraps an ``@cache``.

    Tests that ``monkeypatch.setattr`` on these globals replace them with a
    plain lambda; the autouse fixture's teardown shouldn't crash on the
    way out in that case.
    """
    clear = getattr(fn, "cache_clear", None)
    if clear is not None:
        clear()


@pytest.fixture
def stub_lambda_invoke(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture all calls to ``lambda_client().invoke`` for assertions.

    moto's Lambda mock requires a real function image; stubbing the client
    sidesteps that and lets us assert on the payloads the dispatcher sends
    to ``repo_helper``.
    """
    captured: list[dict[str, Any]] = []

    def fake_invoke(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"Payload": SimpleNamespace(read=lambda: b'{"ok": true}')}

    fake = MagicMock()
    fake.invoke.side_effect = fake_invoke
    h.lambda_client.cache_clear()
    monkeypatch.setattr(h, "lambda_client", lambda: fake)
    return captured


@pytest.fixture
def stub_classify(monkeypatch: pytest.MonkeyPatch) -> Callable[[TriageVerdict], None]:
    """Replace ``classify`` so tests can inject any verdict shape."""

    def install(verdict: TriageVerdict) -> None:
        monkeypatch.setattr(h, "classify", lambda **_kwargs: verdict)

    return install


def base_request_payload() -> dict[str, Any]:
    return {
        "repo": "smoketurner/ai-dlc",
        "issue_number": 42,
        "issue_url": "https://github.com/smoketurner/ai-dlc/issues/42",
        "title": "Add /version endpoint",
        "body": "Return container SHA from IMAGE_SHA env var.",
        "labels": ["aidlc:ready"],
        "user": "alice",
    }


def test_go_emits_request_received_and_comments(
    stub_classify: Callable[[TriageVerdict], None],
    stub_lambda_invoke: list[dict[str, Any]],
) -> None:
    stub_classify(
        TriageVerdict(
            decision="go",
            intent="Add a GET /version endpoint that reads IMAGE_SHA.",
            reasoning="Small additive feature, two-way door.",
        ),
    )

    out = h.handler(base_request_payload(), ctx())

    assert out["ok"] is True
    assert out["decision"] == "go"
    assert out["run_id"]

    # The Lambda made two repo_helper invocations: comment then label.
    assert len(stub_lambda_invoke) == 2
    ops = [json.loads(call["Payload"].decode())["input"]["op"] for call in stub_lambda_invoke]
    assert ops == ["comment_issue", "label_issue"]
    label_call = json.loads(stub_lambda_invoke[1]["Payload"].decode())["input"]
    assert label_call["labels"] == [IN_PROGRESS_LABEL]


def test_defer_posts_comment_and_labels_deferred(
    stub_classify: Callable[[TriageVerdict], None],
    stub_lambda_invoke: list[dict[str, Any]],
) -> None:
    stub_classify(
        TriageVerdict(
            decision="defer",
            reasoning="Blocked on issue #41 which adds the IMAGE_SHA env var.",
        ),
    )

    out = h.handler(base_request_payload(), ctx())

    assert out == {"ok": True, "decision": "defer"}
    label_call = json.loads(stub_lambda_invoke[-1]["Payload"].decode())["input"]
    assert label_call["op"] == "label_issue"
    assert label_call["labels"] == [DEFERRED_LABEL]


def test_decline_posts_reasoning_and_labels_declined(
    stub_classify: Callable[[TriageVerdict], None],
    stub_lambda_invoke: list[dict[str, Any]],
) -> None:
    stub_classify(
        TriageVerdict(
            decision="decline",
            reasoning="Out of scope: the platform doesn't manage CDNs.",
        ),
    )

    out = h.handler(base_request_payload(), ctx())

    assert out == {"ok": True, "decision": "decline"}
    label_call = json.loads(stub_lambda_invoke[-1]["Payload"].decode())["input"]
    assert label_call["labels"] == [DECLINED_LABEL]


def test_validation_error_returns_envelope(
    stub_lambda_invoke: list[dict[str, Any]],
) -> None:
    out = h.handler({"repo": "bad-format", "issue_number": 1}, ctx())

    assert out == {"ok": False, "error": "validation_error"}
    assert stub_lambda_invoke == []


def test_classification_failure_returns_envelope(
    monkeypatch: pytest.MonkeyPatch,
    stub_lambda_invoke: list[dict[str, Any]],
) -> None:
    def boom(**_kwargs: Any) -> TriageVerdict:
        msg = "model returned junk"
        raise ValueError(msg)

    monkeypatch.setattr(h, "classify", boom)

    out = h.handler(base_request_payload(), ctx())

    assert out == {"ok": False, "error": "classification_failed"}
    assert stub_lambda_invoke == []


def test_extract_json_handles_fenced_blocks() -> None:
    text = 'Here is the verdict:\n```json\n{"decision": "go", "reasoning": "ok"}\n```'

    parsed = extract_json(text)

    assert parsed == {"decision": "go", "reasoning": "ok"}


def test_extract_text_concatenates_text_blocks() -> None:
    response = {
        "output": {
            "message": {
                "content": [
                    {"text": "first "},
                    {"text": "second"},
                    {"toolUse": {"name": "ignored"}},
                ],
            },
        },
    }
    assert extract_text(response) == "first second"


def test_render_user_message_includes_labels_and_body() -> None:
    rendered = render_user_message(
        repo="o/r",
        issue_number=7,
        title="t",
        body="b",
        labels=["aidlc:ready"],
    )
    assert "Issue 7 on o/r" in rendered
    assert "aidlc:ready" in rendered
    assert "b" in rendered


def test_request_validation_rejects_bad_issue_url() -> None:
    bad = base_request_payload() | {"issue_url": "ftp://example.com/issues/1"}
    with pytest.raises(ValidationError):
        TriageRequest.model_validate(bad)
