"""Tests for architect.plan — summary + ADR extraction from a plan body."""

from __future__ import annotations

from architect.plan import (
    SECTION_HEADINGS,
    extract_proposed_adrs,
    extract_summary,
)

SAMPLE_PLAN = """\
## Context

Add a /healthz liveness endpoint to the dashboard service so the oncall
can confirm liveness without authenticating.

## Assumptions

- The dashboard is the only public-facing service that lacks /healthz.
- We can return JSON with no body schema.

## Approach

Mount a new FastAPI route at /healthz returning {ok: true}.

## Files to modify / create

- services/dashboard/src/dashboard/routes/health.py:1

## Reuse, don't reinvent

- The existing FastAPI router pattern in routes/auth.py.

## Implementation steps

- [ ] Add health.py with /healthz route.
- [ ] Register the route in app.py.
- [ ] Add unit test.

## Verification

Run `uv run pytest -q services/dashboard/tests/test_health.py`.

## Out of scope

- Readiness probes.
- Build SHA in the body — see docs/ADRs/0007-healthz.md for follow-up.
"""


def test_section_headings_in_canonical_order() -> None:
    expected = (
        "## Context",
        "## Assumptions",
        "## Approach",
        "## Files to modify / create",
        "## Reuse, don't reinvent",
        "## Implementation steps",
        "## Verification",
        "## Out of scope",
    )
    assert expected == SECTION_HEADINGS


def test_extract_summary_returns_first_paragraph_under_context() -> None:
    out = extract_summary(SAMPLE_PLAN)
    assert "Add a /healthz liveness endpoint" in out
    # Stops at the next heading / blank line — does not bleed into Assumptions.
    assert "Assumptions" not in out


def test_extract_summary_caps_at_max_len() -> None:
    body = "## Context\n\n" + ("x" * 5000) + "\n"
    out = extract_summary(body, max_len=64)
    assert len(out) == 64


def test_extract_summary_empty_for_empty_body() -> None:
    assert extract_summary("") == ""


def test_extract_summary_falls_back_to_first_paragraph_when_no_context_heading() -> None:
    body = "Some preamble paragraph.\n\nA second paragraph.\n"
    out = extract_summary(body)
    assert out == "Some preamble paragraph."


def test_extract_proposed_adrs_picks_adr_paths() -> None:
    adrs = extract_proposed_adrs(SAMPLE_PLAN)
    assert adrs == ["docs/ADRs/0007-healthz.md"]


def test_extract_proposed_adrs_dedupes() -> None:
    body = (
        "See docs/ADRs/0007-healthz.md and also "
        "docs/ADRs/0007-healthz.md plus docs/ADRs/0008-auth.md."
    )
    adrs = extract_proposed_adrs(body)
    assert adrs == ["docs/ADRs/0007-healthz.md", "docs/ADRs/0008-auth.md"]


def test_extract_proposed_adrs_empty_when_no_adrs_referenced() -> None:
    assert extract_proposed_adrs("## Context\n\nNo ADRs here.\n") == []
