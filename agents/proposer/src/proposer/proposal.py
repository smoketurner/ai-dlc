"""Pydantic models + helpers for the Proposer's structured output.

The Proposer emits a :class:`Proposal` describing zero or more
:class:`FileEdit` edits to safe target paths. Edit targets are limited
to the project's memory files — ``MEMORY.md`` or ``AGENTS.md``, at the
repo root or under ``docs/``. Anything else is rejected by Pydantic
validation.

The agents themselves should be portable across projects; their
behaviour is steered by the target repo's ``MEMORY.md`` / ``AGENTS.md``
and not by editing agent prompt files. Agent prompt evolution lives in
the agent platform's own repo, separate from any target project.

Strands' structured-output mode surfaces Pydantic errors to the agent
so it can self-correct.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.validators import NoneSafeList

# Allowed: MEMORY.md or AGENTS.md, at the repo root or under docs/.
ALLOWED_TARGETS = re.compile(r"^(docs/)?(MEMORY|AGENTS)\.md$")


class _Frozen(BaseModel):
    """Strict, frozen base for proposal models."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class FileEdit(_Frozen):
    """One file edit the Proposer wants to land via PR."""

    target_file: Annotated[str, Field(min_length=1, max_length=256)]
    proposed_content: Annotated[str, Field(min_length=1, max_length=200_000)]

    @field_validator("target_file")
    @classmethod
    def target_must_be_allowed(cls, v: str) -> str:
        """Restrict targets to the project's MEMORY.md / AGENTS.md only."""
        if not ALLOWED_TARGETS.match(v):
            msg = (
                f"target_file {v!r} is not in the Proposer's allowed set "
                "(MEMORY.md or AGENTS.md, at the repo root or under docs/)"
            )
            raise ValueError(msg)
        return v


class ProposedIssue(_Frozen):
    """One scoped GitHub issue to spawn from a research synthesis.

    Emitted by the Proposer only when the human's triggering comment
    explicitly asks for issue creation based on prior research findings;
    the runtime turns each entry into a single ``repo_helper.create_issue``
    call backlinked to the parent issue.
    """

    title: Annotated[str, Field(min_length=1, max_length=128)]
    body: Annotated[str, Field(min_length=1, max_length=8192)]
    labels: Annotated[NoneSafeList[str], Field(max_length=8)] = Field(default_factory=list)


class Proposal(_Frozen):
    """The Proposer's full structured output.

    An empty ``edits`` list means the Proposer judged the signals
    insufficient to warrant a change; ``rationale`` still explains why.

    For research-trigger runs (issue-driven), :attr:`summary_comment`
    holds the synthesis the Proposer posts as a comment on the source
    issue. For schedule / regression runs it stays empty.
    """

    rationale: Annotated[str, Field(min_length=1, max_length=4096)]
    supporting_evidence: Annotated[NoneSafeList[str], Field(max_length=32)] = Field(
        default_factory=list,
    )
    edits: Annotated[NoneSafeList[FileEdit], Field(max_length=8)] = Field(default_factory=list)
    pr_title: Annotated[str, Field(min_length=1, max_length=72)] = "proposer: no-op"
    pr_body: Annotated[str, Field(min_length=1, max_length=65_536)] = "no edits"
    summary_comment: Annotated[str, Field(max_length=8192)] = ""
    proposed_issues: Annotated[NoneSafeList[ProposedIssue], Field(max_length=16)] = Field(
        default_factory=list,
    )
