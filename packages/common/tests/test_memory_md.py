"""Tests for ``common.memory_md``."""

from __future__ import annotations

import pytest

from common.errors import MemoryDocParseError
from common.memory_md import MemoryDoc, parse, render

_MINIMAL = """# Project Memory

Intro paragraph.

## Overview

ai-dlc is the agentic SDLC platform.

## Conventions

- Python 3.14.

## Decisions

- ADR-0001: use uv.

## Constraints

- Python 3.14 minimum.

## Glossary

- ADR — Architectural Decision Record.

## Notes

(none)
"""


def test_parse_minimal_valid() -> None:
    doc = parse(_MINIMAL)
    assert doc.title == "Project Memory"
    assert doc.intro == "Intro paragraph."
    assert doc.sections["overview"].startswith("ai-dlc")
    assert "ADR-0001" in doc.sections["decisions"]
    assert "Python 3.14 minimum" in doc.sections["constraints"]


def test_render_round_trip_is_stable() -> None:
    doc = parse(_MINIMAL)
    rendered = render(doc)
    again = parse(rendered)
    assert doc == again


def test_unknown_section_fails_fast() -> None:
    bad = _MINIMAL + "\n## SecretLore\n\nshould not be allowed\n"
    with pytest.raises(MemoryDocParseError):
        parse(bad)


def test_out_of_order_sections_fail() -> None:
    swapped = _MINIMAL.replace("## Overview", "## Conventions").replace(
        "## Conventions\n\n- Python 3.14.",
        "## Overview\n\nai-dlc is the agentic SDLC platform.",
    )
    with pytest.raises(MemoryDocParseError):
        parse(swapped)


def test_with_section_replaces() -> None:
    doc = parse(_MINIMAL)
    updated = doc.with_section("notes", "Newly added note.")
    assert updated.sections["notes"] == "Newly added note."
    # Other sections are untouched.
    assert updated.sections["overview"] == doc.sections["overview"]


def test_with_appended_preserves_existing() -> None:
    doc = parse(_MINIMAL)
    updated = doc.with_appended("decisions", "- ADR-0002: choose ECS over App Runner.")
    assert "ADR-0001: use uv." in updated.sections["decisions"]
    assert "ADR-0002" in updated.sections["decisions"]


def test_empty_doc_renders_all_six_headers() -> None:
    rendered = render(MemoryDoc())
    for header in (
        "## Overview",
        "## Conventions",
        "## Decisions",
        "## Constraints",
        "## Glossary",
        "## Notes",
    ):
        assert header in rendered
