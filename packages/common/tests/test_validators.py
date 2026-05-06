"""Tests for ``common.validators``."""

from __future__ import annotations

from typing import Annotated

import pytest
from pydantic import BaseModel, Field, ValidationError

from common.validators import NoneSafeList, none_to_empty_list


class _Inner(BaseModel):
    """Stand-in nested model for typed-list tests."""

    name: str


class _Owner(BaseModel):
    """Container that exercises ``NoneSafeList`` end-to-end."""

    tags: NoneSafeList[str] = Field(default_factory=list)
    items: Annotated[NoneSafeList[_Inner], Field(max_length=4)] = Field(default_factory=list)


def test_none_to_empty_list_handles_none() -> None:
    assert none_to_empty_list(None) == []


def test_none_to_empty_list_passthrough_for_real_list() -> None:
    assert none_to_empty_list(["a", "b"]) == ["a", "b"]


def test_none_to_empty_list_decodes_json_string() -> None:
    assert none_to_empty_list('["a", "b"]') == ["a", "b"]


def test_none_to_empty_list_decodes_json_null_string() -> None:
    assert none_to_empty_list("null") == []


def test_none_to_empty_list_returns_string_unchanged_when_not_json() -> None:
    assert none_to_empty_list("not json") == "not json"


def test_none_safe_list_str_coerces_none() -> None:
    assert _Owner(tags=None).tags == []


def test_none_safe_list_str_default_when_omitted() -> None:
    assert _Owner().tags == []


def test_none_safe_list_str_passthrough() -> None:
    assert _Owner(tags=["a", "b"]).tags == ["a", "b"]


def test_none_safe_list_typed_coerces_none() -> None:
    assert _Owner(items=None).items == []


def test_none_safe_list_typed_validates_inner_model() -> None:
    owner = _Owner(items=[{"name": "a"}, {"name": "b"}])
    assert [i.name for i in owner.items] == ["a", "b"]


def test_none_safe_list_respects_outer_field_constraints() -> None:
    """Outer ``Field(max_length=4)`` still applies after the BeforeValidator runs."""
    with pytest.raises(ValidationError):
        _Owner(items=[{"name": str(i)} for i in range(5)])
