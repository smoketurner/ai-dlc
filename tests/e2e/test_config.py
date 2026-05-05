"""Unit tests for SmokeTestConfig."""

import pytest
from pydantic import ValidationError

from tests.e2e.config import SmokeTestConfig


def test_defaults() -> None:
    cfg = SmokeTestConfig()
    assert str(cfg.ingest_endpoint) == "http://localhost:8080/ingest"
    assert cfg.delivery_sink_arn == ""
    assert cfg.aws_region == "us-east-1"
    assert cfg.timeout_seconds == 60.0


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGEST_ENDPOINT", "https://api.example.com/ingest")
    monkeypatch.setenv("DELIVERY_SINK_ARN", "arn:aws:sqs:us-east-1:123456789012:my-queue")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("TIMEOUT_SECONDS", "30")

    cfg = SmokeTestConfig()
    assert str(cfg.ingest_endpoint) == "https://api.example.com/ingest"
    assert cfg.delivery_sink_arn == "arn:aws:sqs:us-east-1:123456789012:my-queue"
    assert cfg.aws_region == "eu-west-1"
    assert cfg.timeout_seconds == 30.0


def test_timeout_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIMEOUT_SECONDS", "-5")
    with pytest.raises(ValidationError):
        SmokeTestConfig()


def test_frozen() -> None:
    cfg = SmokeTestConfig()
    with pytest.raises(ValidationError):
        cfg.aws_region = "ap-southeast-1"  # type: ignore[misc]
