"""The Architect's plan-document contract.

The Architect produces a single Markdown document at::

  s3://{artifacts_bucket}/runs/{run_id}/plan.md

The body is structured like a Claude Code plan-mode plan: Context →
Assumptions → Approach → Files → Reuse → Implementation steps →
Verification → Out of scope. Section headings are fixed; the platform
hands the body verbatim to the implementer.

Architects produce the body by calling the gateway-routed
``put_artifact(key='runs/{run_id}/plan.md', content=...)`` operation
exactly once. The platform extracts a short summary (first line of the
Context section) and any proposed ADR references for the
``DESIGN.READY`` event payload.
"""

from __future__ import annotations

import re

# Section headings the platform expects in the rendered plan body, in
# the canonical order. Used by :func:`extract_summary` to find the
# Context section.
SECTION_HEADINGS: tuple[str, ...] = (
    "## Context",
    "## Assumptions",
    "## Approach",
    "## Files to modify / create",
    "## Reuse, don't reinvent",
    "## Implementation steps",
    "## Verification",
    "## Out of scope",
)

ADR_PATTERN = re.compile(r"(?i)docs/ADRs/[\w\-./]+\.md")
"""Best-effort matcher for ADR file references in the plan body."""


def extract_summary(plan_body: str, *, max_len: int = 2048) -> str:
    """Return a short summary of the plan — the first paragraph after Context.

    Walks the rendered markdown for the ``## Context`` heading and grabs
    the first non-empty paragraph below it. Falls back to the first
    non-heading paragraph in the document if the heading is missing,
    and to an empty string when the body is empty.

    Args:
        plan_body: Full markdown body produced by the architect.
        max_len: Cap on the returned summary length.

    Returns:
        A single-paragraph summary suitable for the DESIGN.READY event.
    """
    lines = plan_body.splitlines()
    paragraph = _first_paragraph_after(lines, heading="## Context")
    if not paragraph:
        paragraph = _first_paragraph_after(lines, heading=None)
    return paragraph[:max_len]


def _first_paragraph_after(lines: list[str], *, heading: str | None) -> str:
    """Return the first non-empty paragraph after ``heading`` (or top of file)."""
    started = heading is None
    buffer: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not started:
            if line.strip() == heading:
                started = True
            continue
        if line.startswith("#"):
            if buffer:
                break
            continue
        if not line.strip():
            if buffer:
                break
            continue
        buffer.append(line.strip())
    return " ".join(buffer).strip()


def extract_proposed_adrs(plan_body: str) -> list[str]:
    """Return ADR markdown paths referenced anywhere in the plan body.

    Deduped, order-preserving. ADR proposals are advisory metadata for
    the dashboard; the implementer reads the plan body for authoritative
    direction.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in ADR_PATTERN.findall(plan_body):
        if match not in seen:
            seen.add(match)
            out.append(match)
    return out
