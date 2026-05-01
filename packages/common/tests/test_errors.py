"""Tests for ``common.errors``."""

from __future__ import annotations

import pytest

from common.errors import (
    AgentCoreMemoryError,
    AidlcError,
    ConfigurationError,
    CostLimitExceededError,
    MemoryDocParseError,
    ValidationError,
)


def test_message_only() -> None:
    err = ConfigurationError("bus name missing")
    assert str(err) == "bus name missing"
    assert err.context == {}


def test_message_with_context() -> None:
    err = AgentCoreMemoryError("create_event failed", memory_id="m-1", session_id="s-1")
    rendered = str(err)
    assert "create_event failed" in rendered
    assert "memory_id='m-1'" in rendered
    assert "session_id='s-1'" in rendered


def test_subclasses_inherit_base() -> None:
    for subclass in (
        ConfigurationError,
        ValidationError,
        MemoryDocParseError,
        CostLimitExceededError,
    ):
        assert issubclass(subclass, AidlcError)


def test_memory_doc_parse_is_validation_error() -> None:
    assert issubclass(MemoryDocParseError, ValidationError)


def test_can_catch_all_with_base() -> None:
    with pytest.raises(AidlcError):
        raise CostLimitExceededError("over budget", run_id="r-1", spent_usd=12.5)
