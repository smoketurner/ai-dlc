"""Structured output schemas for the Retrospector agent.

The Retrospector runs in two modes and emits a different schema for
each:

* ``capture`` (one invocation per PR-signal event) → :class:`CaptureDecision`
  with zero or more :class:`LessonBullet` entries appended verbatim to
  the destination's pending buffer in S3. No PR is opened.
* ``consolidate`` (fanned out by a weekly scheduled rule, one
  invocation per destination) → :class:`ConsolidationPlan` with
  per-scope MEMORY.md additions, optional SKILL.md files, and the
  buffer contents that didn't make this batch (carried forward to
  next week).

Two destinations are supported:

* ``target_repo`` — repo-specific lessons land in the target repo
  (``MEMORY.md`` per-directory scopes, ``.aidlc/skills/<slug>.md``).
* ``platform`` — agent-friction / validator-pattern / missing-tool
  lessons land in the ai-dlc repo (``MEMORY.md`` /  ``AGENTS.md`` /
  ``.claude/skills/<slug>.md``).

Two artifact types are supported:

* ``memory_md`` — strict six-section schema, picks a section.
* ``skill_md`` — agentskills.io-style file (``name`` +
  ``description`` frontmatter; ``body`` is optional, loaded on
  demand by agents).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Mirrors ``common.memory_md.Section`` — duplicated so the agent's
# Pydantic schema doesn't pull a runtime dependency on the parser.
Section = Literal[
    "overview",
    "conventions",
    "decisions",
    "constraints",
    "glossary",
    "notes",
]

Destination = Literal["target_repo", "platform"]
ArtifactType = Literal["memory_md", "skill_md"]

SCORE_MIN = 1
SCORE_MAX = 5


class LessonBullet(BaseModel):
    """One scored, scoped lesson candidate emitted in capture mode.

    Appended verbatim to the pending-lessons buffer for its
    ``destination``. Consolidate mode reads the buffer, ranks bullets
    holistically, and decides which to ship into a PR.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    destination: Destination = Field(
        description=(
            "Where this lesson belongs: ``target_repo`` for "
            "repo-specific conventions, gotchas, and code patterns; "
            "``platform`` for validator false-positive patterns, "
            "agent-friction signals, missing-tool symptoms — anything "
            "that should improve ai-dlc itself rather than the target "
            "repo."
        ),
    )
    artifact_type: ArtifactType = Field(
        description=(
            "``memory_md`` for a bullet appended under a MEMORY.md "
            "section. ``skill_md`` for a packaged multi-step "
            "procedure worth carrying forward as an agent skill "
            "(agentskills.io-style — name + description "
            "frontmatter, optional body)."
        ),
    )
    scope: Annotated[str, Field(min_length=1, max_length=256)] = Field(
        description=(
            "Path relative to the destination repo root. For "
            "``memory_md`` this is a ``MEMORY.md`` file location "
            "(``MEMORY.md``, ``src/api/MEMORY.md``, etc. — pick the "
            "most specific path that fits). For ``skill_md`` this is "
            "the skill *folder* (no ``.md`` suffix, no trailing "
            "slash): ``.aidlc/skills/<slug>`` in target repos, "
            "``.claude/skills/<slug>`` in the ai-dlc platform repo. "
            "The platform appends ``/SKILL.md`` when writing."
        ),
    )
    section: Section | None = Field(
        default=None,
        description=(
            "MEMORY.md section the bullet belongs under: overview / "
            "conventions / decisions / constraints / glossary / "
            "notes. Required when ``artifact_type=memory_md``; must "
            "be None when ``artifact_type=skill_md``."
        ),
    )
    delta: Annotated[str, Field(min_length=1, max_length=400)] = Field(
        description=(
            "The one-line bullet to add. ≤400 chars. Lead with the "
            "rule or fact; the rationale goes in ``rationale``, not "
            "here. For ``skill_md`` this is a one-line summary of "
            "the skill (the full body goes in ``skill_body``)."
        ),
    )
    severity: Annotated[int, Field(ge=SCORE_MIN, le=SCORE_MAX)] = Field(
        description=(
            "How bad if this lesson is ignored on the next similar "
            "run, 1-5. 5 = will cause a run failure or wrong output; "
            "1 = minor stylistic nudge."
        ),
    )
    generalizability: Annotated[int, Field(ge=SCORE_MIN, le=SCORE_MAX)] = Field(
        description=(
            "How many future runs will benefit from this lesson, "
            "1-5. 5 = applies to every run on this repo / every "
            "agent in the platform; 1 = a one-off specific to this "
            "PR."
        ),
    )
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description=(
            "How confident the agent is the lesson generalises, "
            "0.0-1.0. Below 0.5 means treat as speculative."
        ),
    )
    rationale: Annotated[str, Field(min_length=1, max_length=2048)] = Field(
        description=(
            "Why this is a lesson. Quote the validator finding, "
            "reviewer comment, or check log verbatim where useful — "
            "the consolidate pass uses this when writing the PR body."
        ),
    )
    evidence: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=512)]],
        Field(max_length=16),
    ] = Field(
        default_factory=list,
        description=(
            "S3 keys, GitHub URLs, or other pointers a reviewer can "
            "follow to verify the bullet. Capped at 16 entries."
        ),
    )
    skill_name: Annotated[str, Field(max_length=64)] = Field(
        default="",
        description=(
            "agentskills.io ``name`` frontmatter field — short slug "
            "matching the filename stem. Required when "
            "``artifact_type=skill_md``; empty otherwise."
        ),
    )
    skill_description: Annotated[str, Field(max_length=500)] = Field(
        default="",
        description=(
            "agentskills.io ``description`` frontmatter field — one "
            "sentence (≤500 chars) describing when an agent should "
            "load this skill. Required when ``artifact_type=skill_md``; "
            "empty otherwise."
        ),
    )
    skill_body: Annotated[str, Field(max_length=16384)] = Field(
        default="",
        description=(
            "Skill body markdown — the procedure / steps / examples "
            "an agent reads after it decides to load the skill. "
            "Required when ``artifact_type=skill_md``; empty otherwise."
        ),
    )

    @model_validator(mode="after")
    def consistent_artifact_fields(self) -> LessonBullet:
        """Enforce the memory_md / skill_md mutual-exclusion contract."""
        if self.artifact_type == "memory_md":
            self._validate_memory_md_fields()
        else:
            self._validate_skill_md_fields()
        return self

    def _validate_memory_md_fields(self) -> None:
        if self.section is None:
            msg = "artifact_type=memory_md requires section"
            raise ValueError(msg)
        if self.skill_name or self.skill_description or self.skill_body:
            msg = "artifact_type=memory_md must not set skill_* fields"
            raise ValueError(msg)

    def _validate_skill_md_fields(self) -> None:
        if self.section is not None:
            msg = "artifact_type=skill_md must not set section"
            raise ValueError(msg)
        if not self.skill_name.strip():
            msg = "artifact_type=skill_md requires skill_name"
            raise ValueError(msg)
        if not self.skill_description.strip():
            msg = "artifact_type=skill_md requires skill_description"
            raise ValueError(msg)
        if not self.skill_body.strip():
            msg = "artifact_type=skill_md requires skill_body"
            raise ValueError(msg)


class CaptureDecision(BaseModel):
    """Output of capture mode — zero or more bullets to append.

    Empty ``bullets`` is the routine case (clean run, no signal worth
    recording). ``rationale`` is always populated so the dispatcher
    log shows why the agent did or didn't add anything.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    bullets: Annotated[list[LessonBullet], Field(max_length=8)] = Field(
        default_factory=list,
        description=(
            "Zero or more bullets to append to their destinations' "
            "buffers. Cap of 8 per invocation — if you find more, "
            "pick the highest-severity ones; the rest will surface on "
            "the next similar event."
        ),
    )
    rationale: Annotated[str, Field(min_length=1, max_length=2048)] = Field(
        description=("Why this set of bullets (or none). Always populated."),
    )


class MemoryAddition(BaseModel):
    """One MEMORY.md addition emitted by consolidate mode."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scope: Annotated[str, Field(min_length=1, max_length=256)] = Field(
        description="MEMORY.md file path (root or nested per directory).",
    )
    section: Section = Field(description="Which section to append under.")
    addition: Annotated[str, Field(min_length=1, max_length=4096)] = Field(
        description="Text to append under the section header.",
    )


class SkillFile(BaseModel):
    """One agentskills.io-format skill emitted by consolidate mode.

    The platform writes the body to ``<scope>/SKILL.md`` so the file
    layout matches the canonical agentskills.io schema (one slug
    folder per skill, each containing a literal ``SKILL.md``). Future
    iterations can add ``scripts/`` / ``references/`` / ``assets/``
    inside the same folder without breaking the schema.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    scope: Annotated[str, Field(min_length=1, max_length=256)] = Field(
        description=(
            "Slug folder path (no trailing slash, no ``SKILL.md`` "
            "suffix): ``.aidlc/skills/<slug>`` in target repos, "
            "``.claude/skills/<slug>`` in the platform repo. The "
            "platform appends ``/SKILL.md`` when writing the file."
        ),
    )
    name: Annotated[str, Field(min_length=1, max_length=64)] = Field(
        description="agentskills.io frontmatter ``name`` (matches the slug).",
    )
    description: Annotated[str, Field(min_length=1, max_length=500)] = Field(
        description="agentskills.io frontmatter ``description`` (one sentence).",
    )
    body: Annotated[str, Field(min_length=1, max_length=16384)] = Field(
        description="Skill body Markdown — procedure / steps / examples.",
    )

    @model_validator(mode="after")
    def scope_is_slug_folder(self) -> SkillFile:
        """``scope`` must be a folder path, not a file — block stray ``.md`` suffix."""
        if self.scope.endswith(".md") or self.scope.endswith("/"):
            msg = (
                "SkillFile.scope must be a slug folder path (no ``.md`` suffix, "
                "no trailing slash); the platform appends ``/SKILL.md``."
            )
            raise ValueError(msg)
        return self


class ConsolidationPlan(BaseModel):
    """Output of consolidate mode — patches to open as PRs.

    The agent reads the destination's pending-lessons events (one event
    per bullet, in AgentCore Memory) and emits the patches it wants to
    ship plus the event IDs to remove. Bullets whose event IDs appear
    in ``shipped_event_ids`` or ``discarded_event_ids`` are deleted;
    everything else is deferred automatically — no buffer to re-render.

    The platform opens one MEMORY.md PR when ``memory_additions`` is
    non-empty and one SKILL.md PR when ``skill_files`` is non-empty.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    memory_additions: Annotated[list[MemoryAddition], Field(max_length=32)] = Field(
        default_factory=list,
        description=(
            "MEMORY.md additions to ship this batch, grouped per "
            "scope+section. Multiple additions to the same "
            "scope+section are collapsed into one section-edit."
        ),
    )
    skill_files: Annotated[list[SkillFile], Field(max_length=8)] = Field(
        default_factory=list,
        description="SKILL.md files to create in this batch.",
    )
    shipped_event_ids: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=256)]],
        Field(max_length=64),
    ] = Field(
        default_factory=list,
        description=(
            "Event IDs of bullets that materialised into "
            "``memory_additions`` / ``skill_files`` this batch. The "
            "platform deletes these events after the PR is opened."
        ),
    )
    discarded_event_ids: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=256)]],
        Field(max_length=128),
    ] = Field(
        default_factory=list,
        description=(
            "Event IDs of bullets you judged too low-value or noisy "
            "to keep (score < 4 with no recurrence). The platform "
            "deletes these events without opening any PR for them."
        ),
    )
    rationale: Annotated[str, Field(min_length=1, max_length=4096)] = Field(
        description=(
            "Why this set of patches and these deletions. Used in "
            "the PR body so reviewers see your reasoning."
        ),
    )
