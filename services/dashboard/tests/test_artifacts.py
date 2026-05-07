"""Unit tests for ``dashboard.artifacts`` — moto-backed S3."""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from dashboard.artifacts import (
    extract_severity_counts,
    extract_summary,
    parse_critique_md,
    read_critique,
)
from dashboard.deps import s3, settings

ARTIFACTS = "test-artifacts"

CRITIQUE_MD = """# Critique — `smoke-test`

> Issues: **2** high · **3** medium · **1** low

## Summary

The spec is missing acceptance criteria for the failure modes. Several tasks
do not map to requirements.

## Issues

### 1. [high] requirements

**Problem:** Acceptance criteria omitted.

**Recommendation:** Add explicit error-path tests.

## Strengths

- Clear naming.
"""


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set env vars + spin up a moto-mocked S3 with the artifacts bucket."""
    monkeypatch.setenv("AIDLC_ENV", "dev")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AIDLC_BUS_NAME", "test-bus")
    monkeypatch.setenv("AIDLC_RUNS_TABLE", "test-runs")
    monkeypatch.setenv("AIDLC_IDEMPOTENCY_TABLE", "test-idempotency")
    monkeypatch.setenv("AIDLC_ARTIFACTS_BUCKET", ARTIFACTS)
    monkeypatch.setenv(
        "AIDLC_BEACON_QUEUE_URL",
        "https://sqs.us-east-1.amazonaws.com/000000000000/test-beacon",
    )
    monkeypatch.setenv(
        "AIDLC_GITHUB_APP_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:0:secret:app"
    )
    monkeypatch.setenv("AIDLC_GITHUB_WEBHOOK_SECRET_ID", "test-secret")
    monkeypatch.setenv("AIDLC_COGNITO_USER_POOL_ID", "test-pool")
    monkeypatch.setenv("AIDLC_COGNITO_CLIENT_ID", "test-client")
    settings.cache_clear()
    s3.cache_clear()
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket=ARTIFACTS)
        yield
    settings.cache_clear()
    s3.cache_clear()


def put_critique(run_id: str, body: str) -> None:
    s3().put_object(
        Bucket=ARTIFACTS,
        Key=f"runs/{run_id}/critique.md",
        Body=body.encode("utf-8"),
    )


def test_read_critique_parses_full_document() -> None:
    put_critique("run-1", CRITIQUE_MD)

    critique = read_critique("run-1")

    assert critique is not None
    assert critique.high_severity_count == 2
    assert critique.medium_severity_count == 3
    assert critique.low_severity_count == 1
    assert critique.issue_count == 6
    assert "missing acceptance criteria" in critique.summary
    assert "<h1>" in critique.body_html
    assert "Critique" in critique.body_html


def test_read_critique_returns_none_when_missing() -> None:
    assert read_critique("nonexistent-run") is None


def test_extract_severity_counts_handles_zero_counts() -> None:
    body = "# Critique\n\n> Issues: **0** high · **0** medium · **0** low\n"
    assert extract_severity_counts(body) == (0, 0, 0)


def test_extract_severity_counts_returns_zeros_when_header_missing() -> None:
    assert extract_severity_counts("# Critique\n\nNo header here.\n") == (0, 0, 0)


def test_extract_summary_picks_first_paragraph_after_header() -> None:
    body = "## Summary\n\nFirst paragraph here.\n\nSecond paragraph.\n\n## Issues\n"
    assert extract_summary(body) == "First paragraph here."


def test_parse_critique_md_renders_html_without_raw_html_passthrough() -> None:
    body = (
        "# Title\n\n> Issues: **0** high · **0** medium · **0** low\n\n<script>alert(1)</script>\n"
    )

    critique = parse_critique_md(body)

    assert "<script>" not in critique.body_html
    assert "&lt;script&gt;" in critique.body_html
    assert critique.body_html.startswith("<h1>")
