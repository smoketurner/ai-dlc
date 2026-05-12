"""Pydantic models + Markdown renderer for the Code-Critic's adversarial review.

The Code-Critic produces a :class:`Critique` whose Markdown rendering
lands at:

  s3://{artifacts_bucket}/runs/{run_id}/validation/critique-r{N}.md

where ``N`` is the revision number (0 for the first validation pass).
The critique is referenced from the ``CODE_CRITIQUE.READY`` event
payload via ``critique_s3_key`` and is included as an aggregated
input to any implementer revision pass.
"""

from __future__ import annotations

from functools import cache
from typing import Annotated, Literal

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel, ConfigDict, Field

from common.validators import NoneSafeList

Severity = Literal["high", "medium", "low"]


class _Frozen(BaseModel):
    """Strict, frozen base for critique models."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Issue(_Frozen):
    """One issue the Code-Critic identified in the integrated impl PR.

    ``path``, ``symbol``, and ``line`` together anchor the issue to a
    concrete code location in the diff. ``language`` + ``code_excerpt``
    render the offending code as a fenced block.
    """

    severity: Severity
    path: Annotated[str, Field(min_length=1, max_length=256)]
    symbol: Annotated[str, Field(max_length=128)] | None = None
    line: Annotated[int, Field(ge=1)] | None = None
    description: Annotated[str, Field(min_length=1, max_length=1024)]
    recommendation: Annotated[str, Field(min_length=1, max_length=1024)]
    language: Annotated[str, Field(max_length=32)] | None = None
    code_excerpt: Annotated[str, Field(max_length=4096)] | None = None
    references: Annotated[NoneSafeList[str], Field(max_length=8)] = Field(
        default_factory=list,
    )


class Critique(_Frozen):
    """The full adversarial review produced by the Code-Critic per validation pass.

    ``issues`` is required and non-empty: the Code-Critic's job is to
    find at least one thing — an integrated impl that's truly flawless
    is rare, and if found the critic should flag a low-severity polish
    note rather than emit zero issues.
    """

    run_id: Annotated[str, Field(min_length=1, max_length=64)]
    summary: Annotated[str, Field(min_length=1, max_length=2048)]
    issues: Annotated[NoneSafeList[Issue], Field(min_length=1, max_length=64)]
    strengths: NoneSafeList[str] = Field(default_factory=list)


def issue_anchor(issue: Issue) -> str:
    """Format ``path[:line] (symbol)`` for human-readable rendering."""
    anchor = f"{issue.path}:{issue.line}" if issue.line is not None else issue.path
    if issue.symbol:
        return f"{anchor} ({issue.symbol})"
    return anchor


def severity_counts(critique: Critique) -> dict[Severity, int]:
    """Count issues by severity. Missing severities map to zero."""
    counts: dict[Severity, int] = {"high": 0, "medium": 0, "low": 0}
    for issue in critique.issues:
        counts[issue.severity] += 1
    return counts


@cache
def template_env() -> Environment:
    """Cached Jinja environment loading templates from ``code_critic/templates/``."""
    return Environment(
        loader=PackageLoader("code_critic", "templates"),
        autoescape=select_autoescape(disabled_extensions=("md", "j2"), default=False),
        undefined=StrictUndefined,
    )


def render_critique(critique: Critique) -> str:
    """Render the critique as Markdown — used for both the S3 artifact and PR comment."""
    template = template_env().get_template("critique.md.j2")
    body = template.render(
        critique=critique,
        counts=severity_counts(critique),
        anchor=issue_anchor,
    )
    return body.rstrip() + "\n"
