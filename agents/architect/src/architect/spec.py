"""Pydantic models + Markdown renderers for the three-document spec bundle.

The Architect produces a :class:`SpecBundle` whose three documents land at:

  s3://{artifacts_bucket}/specs/{spec_slug}/requirements.md
                                     /design.md
                                     /tasks.md

The bundle is also written to the project repo under ``docs/specs/{spec_slug}/``
when the spec is approved.
"""

from __future__ import annotations

import re
from typing import Annotated, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from common.door import DoorAssessment


def _none_to_empty_list[T](v: list[T] | None) -> list[T]:
    """Coerce ``None`` to ``[]`` for optional list fields.

    Strands' ``structured_output`` sometimes hands us ``None`` for optional
    list fields the model didn't populate; coerce so downstream Pydantic
    validation accepts the SpecBundle.
    """
    return v if v is not None else []


_OptionalStrList = Annotated[list[str], BeforeValidator(_none_to_empty_list)]

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,126}[a-z0-9]$")
TASK_ID_PATTERN = re.compile(r"^T-\d{3,}$")
REQUIREMENT_ID_PATTERN = re.compile(r"^R-\d{3,}$")


class _Frozen(BaseModel):
    """Strict, frozen base for spec models."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class UserStory(_Frozen):
    """One requirement: ``As a {role}, I want {capability} so that {outcome}``."""

    id: Annotated[str, Field(pattern=REQUIREMENT_ID_PATTERN.pattern)]
    role: Annotated[str, Field(min_length=1, max_length=128)]
    capability: Annotated[str, Field(min_length=1, max_length=512)]
    outcome: Annotated[str, Field(min_length=1, max_length=512)]


class AcceptanceCriterion(_Frozen):
    """Testable acceptance criterion linked to a user story."""

    id: Annotated[str, Field(min_length=1, max_length=64)]
    requirement_id: Annotated[str, Field(pattern=REQUIREMENT_ID_PATTERN.pattern)]
    given: Annotated[str, Field(min_length=1, max_length=512)]
    when: Annotated[str, Field(min_length=1, max_length=512)]
    then: Annotated[str, Field(min_length=1, max_length=512)]


class Requirements(_Frozen):
    """Contents of ``requirements.md``."""

    summary: Annotated[str, Field(min_length=1, max_length=1024)]
    user_stories: Annotated[list[UserStory], Field(min_length=1, max_length=64)]
    acceptance_criteria: Annotated[list[AcceptanceCriterion], Field(min_length=1, max_length=256)]
    out_of_scope: _OptionalStrList = Field(default_factory=list)
    open_questions: _OptionalStrList = Field(default_factory=list)


class DesignComponent(_Frozen):
    """One component named in the design's component list."""

    name: Annotated[str, Field(min_length=1, max_length=128)]
    purpose: Annotated[str, Field(min_length=1, max_length=512)]
    location: Annotated[str, Field(min_length=1, max_length=256)]


class Design(_Frozen):
    """Contents of ``design.md``."""

    approach: Annotated[str, Field(min_length=1, max_length=4096)]
    components: Annotated[list[DesignComponent], Field(min_length=1, max_length=32)]
    data_model: Annotated[str, Field(min_length=1, max_length=4096)]
    sequence: Annotated[str, Field(min_length=1, max_length=4096)]
    failure_modes: _OptionalStrList = Field(default_factory=list)
    trade_offs: _OptionalStrList = Field(default_factory=list)
    proposed_adrs: _OptionalStrList = Field(default_factory=list)
    references: _OptionalStrList = Field(default_factory=list)


class Task(_Frozen):
    """One row in ``tasks.md``.

    Each task becomes one PR. ``door`` carries the architect's call on
    reversibility — ``one_way`` tasks open as draft PRs and require a
    human to mark ready before merge. ``depends_on`` lists task IDs that
    must merge first; the Map state in Step Functions sequences PRs
    accordingly.
    """

    id: Annotated[str, Field(pattern=TASK_ID_PATTERN.pattern)]
    title: Annotated[str, Field(min_length=1, max_length=256)]
    implements: Annotated[list[str], Field(min_length=1, max_length=16)]
    touches: _OptionalStrList = Field(default_factory=list)
    done_when: Annotated[str, Field(min_length=1, max_length=1024)]
    door: DoorAssessment = Field(default_factory=DoorAssessment)
    depends_on: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)

    @model_validator(mode="after")
    def depends_on_excludes_self(self) -> Self:
        """A task must not depend on itself."""
        if self.id in self.depends_on:
            msg = f"task {self.id!r} cannot list itself in depends_on"
            raise ValueError(msg)
        return self


class SpecBundle(_Frozen):
    """The full three-document bundle that the Architect produces per session."""

    spec_slug: Annotated[str, Field(pattern=SLUG_PATTERN.pattern, max_length=128)]
    feature_name: Annotated[str, Field(min_length=1, max_length=256)]
    requirements: Requirements
    design: Design
    tasks: Annotated[list[Task], Field(min_length=1, max_length=64)]


def render_requirements(spec: SpecBundle) -> str:
    """Render the spec's requirements document as Markdown."""
    lines = [
        f"# Requirements — {spec.feature_name}",
        "",
        f"> **Spec slug:** `{spec.spec_slug}`",
        "",
        "## Summary",
        "",
        spec.requirements.summary,
        "",
        "## User stories",
        "",
    ]
    for story in spec.requirements.user_stories:
        lines.append(
            f"- **{story.id}** — As a {story.role}, I want {story.capability} "
            f"so that {story.outcome}.",
        )
    lines += ["", "## Acceptance criteria", ""]
    for ac in spec.requirements.acceptance_criteria:
        lines.append(
            f"- **{ac.id}** ({ac.requirement_id}) — Given {ac.given}, "
            f"when {ac.when}, then {ac.then}.",
        )
    if spec.requirements.out_of_scope:
        lines += ["", "## Out of scope", ""]
        lines += [f"- {item}" for item in spec.requirements.out_of_scope]
    if spec.requirements.open_questions:
        lines += ["", "## Open questions", ""]
        lines += [f"- {item}" for item in spec.requirements.open_questions]
    lines.append("")
    return "\n".join(lines)


def render_design(spec: SpecBundle) -> str:
    """Render the spec's design document as Markdown."""
    d = spec.design
    lines = [
        f"# Design — {spec.feature_name}",
        "",
        f"> **Spec slug:** `{spec.spec_slug}`",
        "",
        "## Approach",
        "",
        d.approach,
        "",
        "## Components",
        "",
    ]
    for c in d.components:
        lines.append(f"- **{c.name}** (`{c.location}`) — {c.purpose}")
    lines += ["", "## Data model", "", "```text", d.data_model, "```", ""]
    lines += ["## Sequence", "", "```text", d.sequence, "```", ""]
    if d.failure_modes:
        lines += ["## Failure modes & mitigations", ""]
        lines += [f"- {item}" for item in d.failure_modes]
        lines.append("")
    if d.trade_offs:
        lines += ["## Trade-offs", ""]
        lines += [f"- {item}" for item in d.trade_offs]
        lines.append("")
    if d.proposed_adrs:
        lines += ["## ADRs proposed", ""]
        lines += [f"- {item}" for item in d.proposed_adrs]
        lines.append("")
    if d.references:
        lines += ["## References", ""]
        lines += [f"- {item}" for item in d.references]
        lines.append("")
    return "\n".join(lines)


def render_tasks(spec: SpecBundle) -> str:
    """Render the spec's tasks document as a Markdown checklist."""
    lines = [
        f"# Tasks — {spec.feature_name}",
        "",
        f"> **Spec slug:** `{spec.spec_slug}`",
        "",
        "Ordered, atomic units. Each task is one PR.",
        "",
    ]
    for task in spec.tasks:
        implements = ", ".join(task.implements)
        lines.append(f"- [ ] **{task.id}** — {task.title}")
        lines.append(f"  - **Implements:** {implements}")
        if task.touches:
            lines.append(f"  - **Touches:** {', '.join(f'`{p}`' for p in task.touches)}")
        if task.depends_on:
            lines.append(f"  - **Depends on:** {', '.join(task.depends_on)}")
        if task.door.door_class == "one_way":
            categories = ", ".join(task.door.categories)
            lines.append(f"  - **Door:** ONE-WAY ({categories}) — {task.door.rationale}")
        lines.append(f"  - **Done when:** {task.done_when}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
