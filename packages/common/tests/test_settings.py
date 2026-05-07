"""Tests for ``common.settings``."""

from __future__ import annotations

import pytest

from common.settings import Settings


def test_defaults_are_dev() -> None:
    settings = Settings()
    assert settings.env == "dev"
    assert settings.region == "us-east-1"
    assert settings.log_level == "INFO"


def test_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDLC_ENV", "prod")
    monkeypatch.setenv("AIDLC_REGION", "us-west-2")
    monkeypatch.setenv("AIDLC_LOG_LEVEL", "DEBUG")
    settings = Settings()
    assert settings.env == "prod"
    assert settings.region == "us-west-2"
    assert settings.log_level == "DEBUG"


def test_frozen_settings_cannot_mutate() -> None:
    settings = Settings()
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError on assignment
        settings.region = "eu-west-1"  # type: ignore[misc]
