"""Pydantic models + helpers for the Proposer's structured output.

The Proposer emits a :class:`Proposal` describing zero or more :class:`FileEdit`
edits to safe target paths (``docs/MEMORY.md`` or
``agents/{name}/src/{name}/prompts.py`` / ``prompts_b.py``). The blast
radius is bounded at the model level — any other target file is rejected
by Pydantic validation, not just by code review.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_TARGETS = re.compile(r"^(docs/MEMORY\.md|agents/[\w-]+/src/[\w-]+/prompts(_b)?\.py)$")


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
        """Restrict targets to MEMORY.md and prompts files only."""
        if not ALLOWED_TARGETS.match(v):
            msg = (
                f"target_file {v!r} is not in the Proposer's allowed set "
                "(docs/MEMORY.md or agents/*/src/*/prompts(_b).py)"
            )
            raise ValueError(msg)
        return v


class Proposal(_Frozen):
    """The Proposer's full structured output.

    An empty ``edits`` list means the Proposer judged the signals
    insufficient to warrant a change; ``rationale`` still explains why.
    """

    rationale: Annotated[str, Field(min_length=1, max_length=4096)]
    supporting_evidence: Annotated[list[str], Field(max_length=32)] = Field(default_factory=list)
    edits: Annotated[list[FileEdit], Field(max_length=8)] = Field(default_factory=list)
    pr_title: Annotated[str, Field(min_length=1, max_length=72)] = "ai-dlc proposer: no-op"
    pr_body: Annotated[str, Field(min_length=1, max_length=65_536)] = "no edits"
