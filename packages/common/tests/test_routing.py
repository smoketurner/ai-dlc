"""Tests for ``common.routing``."""

from __future__ import annotations

import sys
import types

import pytest

from common.routing import (
    load_system_prompt,
    pick_variant,
    variant_actor_id,
)


def test_pick_variant_is_deterministic() -> None:
    a = pick_variant("run-1", "architect")
    b = pick_variant("run-1", "architect")
    assert a == b


def test_pick_variant_differs_across_runs() -> None:
    """A handful of runs should produce both 'a' and 'b' across many runs."""
    seen = {pick_variant(f"run-{i:03d}", "architect") for i in range(40)}
    assert seen == {"a", "b"}


def test_pick_variant_differs_across_agents_within_run() -> None:
    """Different agents in the same run should not always pick the same variant."""
    seen = {pick_variant("run-1", name) for name in ["a1", "a2", "a3", "a4", "a5", "a6"]}
    assert seen == {"a", "b"}


def test_variant_actor_id_format() -> None:
    assert variant_actor_id("architect", "a") == "architect-a"
    assert variant_actor_id("critic", "b") == "critic-b"


def make_prompt_module(qualified_name: str, prompt: str) -> types.ModuleType:
    """Build a fake prompt module — `setattr` keeps ty happy on dynamic attrs."""
    mod = types.ModuleType(qualified_name)
    mod.__dict__["SYSTEM_PROMPT"] = prompt
    return mod


def test_load_system_prompt_a_uses_default_module(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_a = make_prompt_module("fakeagent.prompts", "A-prompt")
    monkeypatch.setitem(sys.modules, "fakeagent.prompts", fake_a)
    monkeypatch.delitem(sys.modules, "fakeagent.prompts_b", raising=False)
    assert load_system_prompt("fakeagent", "a") == "A-prompt"


def test_load_system_prompt_b_uses_b_module(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_a = make_prompt_module("fakeagent.prompts", "A-prompt")
    fake_b = make_prompt_module("fakeagent.prompts_b", "B-prompt")
    monkeypatch.setitem(sys.modules, "fakeagent.prompts", fake_a)
    monkeypatch.setitem(sys.modules, "fakeagent.prompts_b", fake_b)
    assert load_system_prompt("fakeagent", "b") == "B-prompt"


def test_load_system_prompt_b_falls_back_when_b_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_a = make_prompt_module("fakeagent.prompts", "A-prompt")
    monkeypatch.setitem(sys.modules, "fakeagent.prompts", fake_a)
    monkeypatch.delitem(sys.modules, "fakeagent.prompts_b", raising=False)
    # Even when 'b' is requested, falls back to A if the b module doesn't exist.
    assert load_system_prompt("fakeagent", "b") == "A-prompt"
