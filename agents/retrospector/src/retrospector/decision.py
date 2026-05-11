"""Structured output schema for the Retrospector agent.

The agent reads a closed PR or issue + its comments, looks at the
project's current ``MEMORY.md`` / ``AGENTS.md``, and decides whether
the trace contains a lesson worth persisting. The
:class:`RetrospectiveDecision` is the agent's final answer; the
platform reads ``has_lesson`` to decide whether to open a PR.

Two target files are supported:

* ``MEMORY.md`` — strict six-section schema (overview, conventions,
  decisions, constraints, glossary, notes). The agent must pick a
  section; the platform parses and appends under that header.
* ``AGENTS.md`` — free-form Markdown. The agent's addition is
  appended as a new section / bullet at the end of the file. No
  section field is consulted.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Mirrors ``common.memory_md.Section`` — duplicated here so the agent's
# Pydantic schema doesn't pull a runtime dependency on the parser.
Section = Literal[
    "overview",
    "conventions",
    "decisions",
    "constraints",
    "glossary",
    "notes",
]

TargetFile = Literal["MEMORY.md", "AGENTS.md"]


class RetrospectiveDecision(BaseModel):
    """The retrospector's verdict on one terminal event.

    Either the trace yielded a reusable lesson (``has_lesson=True``)
    and ``memory_md_addition`` is set with the proposed bullet, or
    there's nothing worth recording (``has_lesson=False``) and the
    addition fields are empty.

    The validator enforces the consistency invariant so the platform
    code can branch cleanly on ``has_lesson`` without re-checking.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    has_lesson: bool = Field(
        description=(
            "True when the trace yielded a reusable insight worth "
            "appending to the project's memory files. False when the "
            "outcome was routine (clean merge with no comments, "
            "ordinary close, etc.)."
        ),
    )
    target_file: TargetFile | None = Field(
        default=None,
        description=(
            "Where to write the addition: ``MEMORY.md`` (structured, "
            "six-section schema — pick when the lesson fits one of "
            "those buckets) or ``AGENTS.md`` (free-form, append at "
            "end — pick when the lesson is project-context that "
            "doesn't fit MEMORY.md's taxonomy). Required when "
            "has_lesson is True; must be None when has_lesson is False."
        ),
    )
    section: Section | None = Field(
        default=None,
        description=(
            "Which MEMORY.md section the addition belongs under: "
            "overview / conventions / decisions / constraints / "
            "glossary / notes. Required when target_file is "
            "``MEMORY.md``; must be None for ``AGENTS.md`` and when "
            "has_lesson is False."
        ),
    )
    lesson_summary: Annotated[str, Field(max_length=200)] = Field(
        default="",
        description=(
            "One-sentence summary of the lesson (≤200 chars). Empty when has_lesson is False."
        ),
    )
    memory_md_addition: Annotated[str, Field(max_length=2048)] = Field(
        default="",
        description=(
            "The exact text to append under ``section`` in "
            "``MEMORY.md`` — typically a single bullet, optionally "
            "with a short Why-line below it. Empty when has_lesson "
            "is False."
        ),
    )
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.0,
        description=(
            "How confident the agent is that this lesson generalises "
            "(0.0-1.0). Below 0.5 means treat as speculative; the "
            "platform may still open the PR but the reviewer will "
            "scrutinise harder."
        ),
    )
    rationale: Annotated[str, Field(max_length=2048)] = Field(
        description=(
            "Brief explanation of why this is or isn't a lesson — "
            "always populated, even when has_lesson is False, so the "
            "PR body can quote the agent's reasoning."
        ),
    )

    @model_validator(mode="after")
    def consistent_lesson_fields(self) -> RetrospectiveDecision:
        """Enforce field consistency between has_lesson / target_file / section."""
        if self.has_lesson:
            self._validate_lesson_fields()
        else:
            self._validate_no_lesson_fields()
        return self

    def _validate_lesson_fields(self) -> None:
        """Has-lesson branch: target_file + section + summary + addition required."""
        if self.target_file is None:
            msg = "has_lesson=True requires target_file"
            raise ValueError(msg)
        if self.target_file == "MEMORY.md" and self.section is None:
            msg = "target_file=MEMORY.md requires section"
            raise ValueError(msg)
        if self.target_file == "AGENTS.md" and self.section is not None:
            msg = "target_file=AGENTS.md must not set section (file is free-form)"
            raise ValueError(msg)
        if not self.lesson_summary.strip():
            msg = "has_lesson=True requires a non-empty lesson_summary"
            raise ValueError(msg)
        if not self.memory_md_addition.strip():
            msg = "has_lesson=True requires a non-empty memory_md_addition"
            raise ValueError(msg)

    def _validate_no_lesson_fields(self) -> None:
        """No-lesson branch: every populated field is a contradiction."""
        if self.target_file is not None:
            msg = "has_lesson=False but target_file is set"
            raise ValueError(msg)
        if self.section is not None:
            msg = "has_lesson=False but section is set"
            raise ValueError(msg)
        if self.lesson_summary.strip():
            msg = "has_lesson=False but lesson_summary is non-empty"
            raise ValueError(msg)
        if self.memory_md_addition.strip():
            msg = "has_lesson=False but memory_md_addition is non-empty"
            raise ValueError(msg)
