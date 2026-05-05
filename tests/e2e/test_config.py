"""Unit tests for ``tests.e2e.config``."""

from __future__ import annotations

import pytest

from tests.e2e.config import SmokeTestConfig


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMOKE_AWS_REGION", "us-east-1")
    monkeypatch.setenv("SMOKE_PRODUCT_ID", "prod-123")
    monkeypatch.setenv("SMOKE_ENDPOINT_URL", "https://example.com")
    cfg = SmokeTestConfig()
    assert cfg.aws_region == "us-east-1"
    assert cfg.product_id == "prod-123"
    assert cfg.endpoint_url == "https://example.com"
    assert cfg.timeout_seconds == 300
    assert cfg.teardown_on_failure is True


def test_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMOKE_AWS_REGION", "eu-west-1")
    monkeypatch.setenv("SMOKE_PRODUCT_ID", "prod-456")
    monkeypatch.setenv("SMOKE_ENDPOINT_URL", "https://other.example.com")
    monkeypatch.setenv("SMOKE_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("SMOKE_TEARDOWN_ON_FAILURE", "false")
    cfg = SmokeTestConfig()
    assert cfg.aws_region == "eu-west-1"
    assert cfg.product_id == "prod-456"
    assert cfg.endpoint_url == "https://other.example.com"
    assert cfg.timeout_seconds == 60
    assert cfg.teardown_on_failure is False


def test_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMOKE_AWS_REGION", "us-east-1")
    monkeypatch.setenv("SMOKE_PRODUCT_ID", "prod-123")
    monkeypatch.setenv("SMOKE_ENDPOINT_URL", "https://example.com")
    cfg = SmokeTestConfig()
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError on assignment
        cfg.timeout_seconds = 999  # type: ignore[misc]


def test_missing_required_fields() -> None:
    with pytest.raises(Exception):  # noqa: B017
        SmokeTestConfig()
