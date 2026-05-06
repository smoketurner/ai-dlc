"""Pydantic models + Markdown renderer for the Tester's gap report.

The Tester produces a :class:`Report` whose Markdown rendering lands at:

  s3://{artifacts_bucket}/runs/{run_id}/tasks/{task_id}/test_report.md

Each :class:`Gap` is a behaviour or acceptance criterion the diff exercises
without test coverage. Each :class:`Suggestion` is a concrete proposed test
the implementer can write to close one or more gaps.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from common.validators import NoneSafeList

TestKind = Literal["unit", "integration", "property", "e2e"]


class _Frozen(BaseModel):
    """Strict, frozen base for report models."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Gap(_Frozen):
    """One missing piece of test coverage the Tester identified.

    ``path``, ``symbol``, and ``line`` together anchor the gap to the
    code that lacks coverage. ``symbol`` and ``line`` are optional — a
    gap may be file-level (e.g., "no tests at all for module X").
    """

    path: Annotated[str, Field(min_length=1, max_length=256)]
    symbol: Annotated[str, Field(max_length=128)] | None = None
    line: Annotated[int, Field(ge=1)] | None = None
    description: Annotated[str, Field(min_length=1, max_length=1024)]


class Suggestion(_Frozen):
    """One proposed test to close one or more gaps."""

    name: Annotated[str, Field(min_length=1, max_length=128)]
    test_kind: TestKind
    given: Annotated[str, Field(min_length=1, max_length=512)]
    when: Annotated[str, Field(min_length=1, max_length=512)]
    then: Annotated[str, Field(min_length=1, max_length=512)]
    covers: Annotated[list[str], Field(min_length=1, max_length=16)]


class Report(_Frozen):
    """The full test gap report produced by the Tester per task PR."""

    task_id: Annotated[str, Field(min_length=1, max_length=32)]
    summary: Annotated[str, Field(min_length=1, max_length=2048)]
    gaps: Annotated[NoneSafeList[Gap], Field(max_length=64)] = Field(default_factory=list)
    suggestions: Annotated[NoneSafeList[Suggestion], Field(max_length=64)] = Field(
        default_factory=list,
    )
    strengths: NoneSafeList[str] = Field(default_factory=list)


def gap_anchor(gap: Gap) -> str:
    """Format ``path[:line] (symbol)`` for human-readable rendering."""
    anchor = f"{gap.path}:{gap.line}" if gap.line is not None else gap.path
    if gap.symbol:
        return f"{anchor} ({gap.symbol})"
    return anchor


def gap_count(report: Report) -> int:
    """Total gaps the report identifies."""
    return len(report.gaps)


def suggestion_count(report: Report) -> int:
    """Total tests the report suggests."""
    return len(report.suggestions)


def render_report(report: Report) -> str:
    """Render the report as a Markdown document."""
    lines = [
        f"# Test report — `{report.task_id}`",
        "",
        f"> **{gap_count(report)}** gap(s) · **{suggestion_count(report)}** suggestion(s)",
        "",
        "## Summary",
        "",
        report.summary,
        "",
    ]
    if report.gaps:
        lines += ["## Gaps", ""]
        for ix, gap in enumerate(report.gaps, start=1):
            lines.append(f"{ix}. **{gap_anchor(gap)}** — {gap.description}")
        lines.append("")
    if report.suggestions:
        lines += ["## Suggested tests", ""]
        for ix, suggestion in enumerate(report.suggestions, start=1):
            lines.append(f"### {ix}. `{suggestion.name}` ({suggestion.test_kind})")
            lines.append("")
            lines.append(f"- **Given** {suggestion.given}")
            lines.append(f"- **When** {suggestion.when}")
            lines.append(f"- **Then** {suggestion.then}")
            covers = ", ".join(f"`{c}`" for c in suggestion.covers)
            lines.append(f"- **Covers:** {covers}")
            lines.append("")
    if report.strengths:
        lines += ["## Strengths", ""]
        lines += [f"- {item}" for item in report.strengths]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
