"""Unit tests for the Triage dispatcher Lambda.

The dispatcher delegates classification to the dedicated triage agent
runtime — these tests stub out the runtime + S3 round-trip and verify
the dispatcher correctly maps each :class:`TriageDecision` action to
the right side effects (REQUEST.RECEIVED emit, label change, comment).
"""

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
from triage_dispatcher.handler import (
    AWAITING_RESPONSE_LABEL,
    DECLINED_LABEL,
    DEFERRED_LABEL,
    IN_PROGRESS_LABEL,
)
from triage_dispatcher.models import TriageRequest

from common.runtime import TriageInput
from common.triage import MissingInformation, TriageDecision
from triage_dispatcher import handler as h

BUS = "test-bus"
REPO_HELPER = "test-repo-helper"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:0:runtime/triage"
ARTIFACTS = "ai-dlc-test-artifacts"


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
    """Spin up moto for EventBridge + S3 + stub Lambda + runtime clients."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AIDLC_BUS_NAME", BUS)
    monkeypatch.setenv("AIDLC_REPO_HELPER_FUNCTION_NAME", REPO_HELPER)
    monkeypatch.setenv("AIDLC_TRIAGE_RUNTIME_ARN", RUNTIME_ARN)
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", ARTIFACTS)
    safe_cache_clear(h.events_client)
    safe_cache_clear(h.lambda_client)
    safe_cache_clear(h.runtime_client)
    safe_cache_clear(h.s3_client)
    with mock_aws():
        boto3.client("events").create_event_bus(Name=BUS)
        boto3.client("s3").create_bucket(Bucket=ARTIFACTS)
        yield
    safe_cache_clear(h.events_client)
    safe_cache_clear(h.lambda_client)
    safe_cache_clear(h.runtime_client)
    safe_cache_clear(h.s3_client)


def safe_cache_clear(fn: Any) -> None:
    """Call ``cache_clear`` only if the symbol still wraps an ``@cache``."""
    clear = getattr(fn, "cache_clear", None)
    if clear is not None:
        clear()


@pytest.fixture
def stub_lambda_invoke(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture all calls to ``lambda_client().invoke`` for assertions."""
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
def stub_decision(monkeypatch: pytest.MonkeyPatch) -> Callable[[TriageDecision], None]:
    """Replace ``invoke_triage_runtime`` so tests can inject any decision shape."""

    def install(decision: TriageDecision) -> None:
        monkeypatch.setattr(h, "invoke_triage_runtime", lambda *_args, **_kwargs: decision)

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


def parse_invoke_payload(call: dict[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(call["Payload"].decode())["input"])


def test_proceed_emits_request_received_and_comments(
    stub_decision: Callable[[TriageDecision], None],
    stub_lambda_invoke: list[dict[str, Any]],
) -> None:
    stub_decision(
        TriageDecision(
            action="proceed",
            workflow_kind="spec_driven",
            rationale="Small additive feature; clear acceptance.",
        ),
    )

    out = h.handler(base_request_payload(), ctx())

    assert out["ok"] is True
    assert out["decision"] == "proceed"
    assert out["workflow_kind"] == "spec_driven"
    assert out["run_id"]

    assert len(stub_lambda_invoke) == 2
    ops = [parse_invoke_payload(call)["op"] for call in stub_lambda_invoke]
    assert ops == ["comment_issue", "label_issue"]
    assert parse_invoke_payload(stub_lambda_invoke[1])["labels"] == [IN_PROGRESS_LABEL]


def test_ask_posts_questions_and_labels_awaiting(
    stub_decision: Callable[[TriageDecision], None],
    stub_lambda_invoke: list[dict[str, Any]],
) -> None:
    stub_decision(
        TriageDecision(
            action="ask",
            rationale="Acceptance criteria are missing.",
            missing_information=[
                MissingInformation(
                    question="What status code on auth failure?",
                    why_needed="Picks between 401 and 403.",
                ),
                MissingInformation(
                    question="Should /version include build sha?",
                    why_needed="Determines the response shape.",
                ),
            ],
        ),
    )

    out = h.handler(base_request_payload(), ctx())

    assert out == {"ok": True, "decision": "ask", "question_count": 2}
    comment_body = parse_invoke_payload(stub_lambda_invoke[0])["body"]
    assert "What status code on auth failure?" in comment_body
    assert "Should /version include build sha?" in comment_body
    label_call = parse_invoke_payload(stub_lambda_invoke[1])
    assert label_call["op"] == "label_issue"
    assert label_call["labels"] == [AWAITING_RESPONSE_LABEL]


def test_defer_posts_comment_and_labels_deferred(
    stub_decision: Callable[[TriageDecision], None],
    stub_lambda_invoke: list[dict[str, Any]],
) -> None:
    stub_decision(
        TriageDecision(
            action="defer",
            rationale="Blocked on issue #41 which adds the IMAGE_SHA env var.",
        ),
    )

    out = h.handler(base_request_payload(), ctx())

    assert out == {"ok": True, "decision": "defer"}
    label_call = parse_invoke_payload(stub_lambda_invoke[-1])
    assert label_call["op"] == "label_issue"
    assert label_call["labels"] == [DEFERRED_LABEL]


def test_decline_posts_reasoning_and_labels_declined(
    stub_decision: Callable[[TriageDecision], None],
    stub_lambda_invoke: list[dict[str, Any]],
) -> None:
    stub_decision(
        TriageDecision(
            action="decline",
            rationale="Out of scope: the platform doesn't manage CDNs.",
        ),
    )

    out = h.handler(base_request_payload(), ctx())

    assert out == {"ok": True, "decision": "decline"}
    label_call = parse_invoke_payload(stub_lambda_invoke[-1])
    assert label_call["labels"] == [DECLINED_LABEL]


def test_validation_error_returns_envelope(
    stub_lambda_invoke: list[dict[str, Any]],
) -> None:
    out = h.handler({"repo": "bad-format", "issue_number": 1}, ctx())

    assert out == {"ok": False, "error": "validation_error"}
    assert stub_lambda_invoke == []


def test_runtime_failure_returns_envelope(
    monkeypatch: pytest.MonkeyPatch,
    stub_lambda_invoke: list[dict[str, Any]],
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> TriageDecision:
        raise ValidationError.from_exception_data("TriageDecision", [])

    monkeypatch.setattr(h, "invoke_triage_runtime", boom)
    out = h.handler(base_request_payload(), ctx())

    assert out == {"ok": False, "error": "triage_runtime_failed"}
    assert stub_lambda_invoke == []


def test_request_validation_rejects_bad_issue_url() -> None:
    bad = base_request_payload() | {"issue_url": "ftp://example.com/issues/1"}
    with pytest.raises(ValidationError):
        TriageRequest.model_validate(bad)


def test_invoke_triage_runtime_reads_decision_from_s3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end happy path through the runtime + S3 integration glue."""
    decision = TriageDecision(
        action="proceed",
        workflow_kind="bug_fix",
        rationale="Bug has clear repro.",
    )
    s3_key = "runs/r1/triage.json"
    boto3.client("s3").put_object(
        Bucket=ARTIFACTS,
        Key=s3_key,
        Body=decision.model_dump_json().encode("utf-8"),
    )
    fake_runtime = MagicMock()
    fake_runtime.invoke_agent_runtime.return_value = {
        "response": SimpleNamespace(
            read=lambda: json.dumps(
                {
                    "decision_s3_key": s3_key,
                    "action": "proceed",
                    "workflow_kind": "bug_fix",
                    "rationale": "Bug has clear repro.",
                    "missing_information_count": 0,
                    "confidence": 0.9,
                    "session_id": "r1-triage",
                },
            ).encode("utf-8"),
        ),
    }
    h.runtime_client.cache_clear()
    monkeypatch.setattr(h, "runtime_client", lambda: fake_runtime)

    payload = TriageInput(
        project_slug="ai-dlc",
        target_repo="o/r",
        issue_url="https://github.com/o/r/issues/1",
        issue_number=1,
        issue_title="x",
        issue_body="y",
        run_id="r1",
        correlation_id="c1",
    )
    result = h.invoke_triage_runtime(payload, run_id="r1")
    assert result == decision
    fake_runtime.invoke_agent_runtime.assert_called_once()
