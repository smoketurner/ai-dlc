"""Tests for ``common.steering``."""

from __future__ import annotations

from common.steering import (
    Accept,
    Retry,
    strip_code_fences,
    validate_required_sections,
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
