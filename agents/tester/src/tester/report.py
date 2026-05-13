"""Pydantic models + Markdown renderer for the Tester's gap report.

The Tester produces a :class:`Report` whose Markdown rendering lands at:

  s3://{artifacts_bucket}/runs/{run_id}/validation/test_report-r{N}.md

where ``N`` is the revision number (0 for the first validation pass).
Each :class:`Gap` is a behaviour or acceptance criterion the integrated
impl PR exercises without test coverage. Each :class:`Suggestion` is a
concrete proposed test the implementer can write to close one or more
gaps.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from common.templating import make_template_env
from common.validators import NoneSafeList

TestKind = Literal["unit", "integration", "property", "e2e"]


class _Frozen(BaseModel):
    """Strict, frozen base for report models."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ReportSummary(_Frozen):
    """Structured summary — bulleted in the rendered output.

    ``context`` says what the diff implements; ``coverage_gap`` says
    what behaviour the diff exercises without a test; ``risk`` says
    what could break in production if the gap goes unclosed.
    """

    context: Annotated[str, Field(min_length=1, max_length=1024)]
    coverage_gap: Annotated[str, Field(min_length=1, max_length=1024)]
    risk: Annotated[str, Field(min_length=1, max_length=1024)]


class Gap(_Frozen):
    """One missing piece of test coverage the Tester identified.

    ``path``, ``symbol``, and ``line`` together anchor the gap to the
    code that lacks coverage. ``symbol`` and ``line`` are optional — a
    gap may be file-level (e.g., "no tests at all for module X").

    ``language`` + ``code_excerpt`` render the untested code as a
    fenced block so the reader sees which branch lacks coverage.
    """

    path: Annotated[str, Field(min_length=1, max_length=256)]
    symbol: Annotated[str, Field(max_length=128)] | None = None
    line: Annotated[int, Field(ge=1)] | None = None
    description: Annotated[str, Field(min_length=1, max_length=1024)]
    language: Annotated[str, Field(max_length=32)] | None = None
    code_excerpt: Annotated[str, Field(max_length=4096)] | None = None


class Suggestion(_Frozen):
    """One proposed test to close one or more gaps.

    ``language`` + ``proposed_test_code`` render a runnable test stub
    as a fenced block so the implementer can paste it directly. Omit
    the code when the suggestion is non-textual (e.g., "add a new
    integration test file under tests/integration/").
    """

    name: Annotated[str, Field(min_length=1, max_length=128)]
    test_kind: TestKind
    given: Annotated[str, Field(min_length=1, max_length=512)]
    when: Annotated[str, Field(min_length=1, max_length=512)]
    then: Annotated[str, Field(min_length=1, max_length=512)]
    covers: Annotated[list[str], Field(min_length=1, max_length=16)]
    language: Annotated[str, Field(max_length=32)] | None = None
    proposed_test_code: Annotated[str, Field(max_length=4096)] | None = None
    references: Annotated[NoneSafeList[str], Field(max_length=8)] = Field(
        default_factory=list,
    )


class Report(_Frozen):
    """The full test gap report produced by the Tester per impl-PR validation pass."""

    run_id: Annotated[str, Field(min_length=1, max_length=64)]
    summary: ReportSummary
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
    """Render the report as Markdown — used for both the S3 artifact and PR comment."""
    template = make_template_env(__package__).get_template("test_report.md.j2")
    body = template.render(
        report=report,
        gap_count=gap_count(report),
        suggestion_count=suggestion_count(report),
        anchor=gap_anchor,
    )
    return body.rstrip() + "\n"
