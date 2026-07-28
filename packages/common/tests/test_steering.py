"""Tests for ``common.steering``."""

from __future__ import annotations

from common.steering import (
    Accept,
    Allow,
    Deny,
    Guide,
    Redirect,
    Retry,
    strip_code_fences,
    validate_contains_file_ref,
    validate_min_length,
    validate_one_of,
    validate_required_sections,
)


def test_decision_dataclasses_are_frozen() -> None:
    """Decisions are frozen so they can be cached and compared by value."""
    a = Allow()
    b = Allow()
    assert a == b
    assert hash(a) == hash(b)
    assert Deny("nope") == Deny("nope")
    assert Deny("nope") != Deny("nope!")
    assert Guide("read first") == Guide("read first")
    assert Redirect({"path": "workspace/x"}, "rebased") == Redirect(
        {"path": "workspace/x"}, "rebased"
    )


def test_judge_results_are_frozen() -> None:
    assert Accept() == Accept()
    assert Retry("missing field") == Retry("missing field")
    assert Retry("a") != Retry("b")


def test_required_sections_all_present() -> None:
    text = "# Context\nbody\n# Approach\nbody\n# Verification\nbody"
    missing = validate_required_sections(text, ["Context", "Approach", "Verification"])
    assert missing == []


def test_required_sections_some_missing_returns_in_input_order() -> None:
    text = "# Context\nbody\n# Verification\nbody"
    missing = validate_required_sections(
        text,
        ["Context", "Approach", "Out of scope", "Verification"],
    )
    assert missing == ["Approach", "Out of scope"]


def test_required_sections_case_insensitive() -> None:
    text = "# context\n# APPROACH"
    assert validate_required_sections(text, ["Context", "Approach"]) == []


def test_required_sections_respects_heading_level() -> None:
    text = "# Wrong level\n## Context\n## Approach"
    assert validate_required_sections(text, ["Context"], heading_level=2) == []
    assert validate_required_sections(text, ["Context"], heading_level=1) == ["Context"]


FENCED_ONLY_PLAN = """\
Here is the template I was asked to follow:

```markdown
## Context
## Approach
## Verification
```

I will fill it in later.
"""


def test_required_sections_ignores_headings_inside_code_fences() -> None:
    """A plan that only *quotes* the template must not satisfy the gate."""
    missing = validate_required_sections(
        FENCED_ONLY_PLAN,
        ["Context", "Approach", "Verification"],
        heading_level=2,
    )
    assert missing == ["Context", "Approach", "Verification"]


def test_required_sections_counts_real_headings_outside_fences() -> None:
    text = "## Context\n\n```python\n# Approach\n```\n\n## Verification\n"
    missing = validate_required_sections(
        text,
        ["Context", "Approach", "Verification"],
        heading_level=2,
    )
    assert missing == ["Approach"]


def test_required_sections_handles_tilde_fences() -> None:
    text = "~~~\n## Context\n~~~\n"
    assert validate_required_sections(text, ["Context"], heading_level=2) == ["Context"]


def test_required_sections_handles_unclosed_fence() -> None:
    text = "## Context\n\n```\n## Approach\n"
    missing = validate_required_sections(text, ["Context", "Approach"], heading_level=2)
    assert missing == ["Approach"]


def test_strip_code_fences_preserves_line_count() -> None:
    text = "a\n```\nb\n```\nc"
    assert strip_code_fences(text).splitlines() == ["a", "", "", "", "c"]


def test_strip_code_fences_leaves_fenceless_text_alone() -> None:
    text = "## Context\n\nJust prose."
    assert strip_code_fences(text) == text


def test_strip_code_fences_does_not_close_on_a_shorter_fence() -> None:
    """A ```` opener is not closed by an inner ``` line."""
    text = "````\n```\n## Context\n````\n## Approach"
    assert validate_required_sections(text, ["Context", "Approach"], heading_level=2) == ["Context"]


def test_required_sections_ignores_inline_hash() -> None:
    """Inline ``#`` (e.g. in code blocks or prose) must not match a heading."""
    text = "Use # for issue numbers like #123 in commits."
    assert validate_required_sections(text, ["Context"]) == ["Context"]


def test_min_length_passes_above_threshold() -> None:
    assert validate_min_length("hello world", 5) == []


def test_min_length_strips_whitespace_before_counting() -> None:
    errors = validate_min_length("   hi   ", 5)
    assert errors == ["output too short: 2 chars, need at least 5"]


def test_one_of_accepts_valid_value() -> None:
    assert validate_one_of("proceed", ["proceed", "ask", "defer"]) == []


def test_one_of_strips_quotes_and_whitespace() -> None:
    assert validate_one_of('  "proceed"  ', ["proceed", "ask"]) == []
    assert validate_one_of("`ask`", ["proceed", "ask"]) == []


def test_one_of_rejects_unknown_value() -> None:
    errors = validate_one_of("perhaps", ["proceed", "ask"])
    assert errors == ["value 'perhaps' not one of ['proceed', 'ask']"]


def test_contains_file_ref_with_path() -> None:
    assert validate_contains_file_ref("see packages/common/src/common/hooks.py") == []


def test_contains_file_ref_with_line_number() -> None:
    assert validate_contains_file_ref("bug at agents/architect/src/architect/agent.py:42") == []


def test_contains_file_ref_missing() -> None:
    errors = validate_contains_file_ref("looks fine to me, no concerns")
    assert errors == ["output contains no file path reference"]
