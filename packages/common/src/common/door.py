"""One-way / two-way door classification for autonomous PR merges.

Two-way door changes are reversible — they merge on advisory review.
One-way door changes cause the Implementer to open the PR in *draft*
mode; a human must mark it ready for review before it can be merged.

Detection is layered, defense in depth:

  * The Architect emits a :class:`DoorAssessment` per task in the spec
    bundle, based on planned scope.
  * The Critic reviews the spec and may upgrade ``two_way -> one_way``
    when it spots irreversibility the Architect missed.
  * The Reviewer re-checks against the actual diff at PR time using the
    path-and-content rules in :func:`classify_paths`; can upgrade.
  * A ``PreToolUse`` hook on ``open_pr`` enforces a hard floor — if any
    of the path patterns match and the agent's stated ``door_class`` is
    still ``two_way``, the call is denied.

Path-based classification covers the seven categories whose signature
shows up in file paths. The other three categories
(``public_api_break``, ``major_dependency_bump``, ``public_deletion``)
require diff-content analysis and are the agent's responsibility.

The ten one-way categories are the canonical list committed in the
project's `CLAUDE.md` and `docs/aws-agent-architecture-guide.md`.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DoorClass = Literal["one_way", "two_way"]
"""Reversibility class of a unit of work."""

OneWayCategory = Literal[
    "schema_migration",
    "public_api_break",
    "production_terraform",
    "iam_authorization",
    "auth_flow",
    "cryptography_or_secrets",
    "major_dependency_bump",
    "scheduled_job",
    "event_schema_breaking",
    "public_deletion",
]
"""Specific kind of one-way door change. See :func:`classify_paths`."""


_PATH_RULES: tuple[tuple[OneWayCategory, re.Pattern[str]], ...] = (
    ("production_terraform", re.compile(r"^terraform/envs/prod/")),
    ("schema_migration", re.compile(r"(?:^|/)migrations/|(?:^|/)[^/]*schema[^/]*\.sql$")),
    ("iam_authorization", re.compile(r"(?:^|/)iam\.tf$|(?:^|/)policies/|.*-policy\.json$")),
    ("event_schema_breaking", re.compile(r"^terraform/shared/schemas/[^/]+\.json$")),
    ("cryptography_or_secrets", re.compile(r"(?:^|/)kms\.tf$|(?:^|/)secrets\.tf$")),
    ("auth_flow", re.compile(r"(?:^|/)cognito\.tf$|(?:^|/)auth\.tf$")),
    ("scheduled_job", re.compile(r"(?:^|/)cron\.tf$|(?:^|/)schedule\.tf$")),
)


def classify_paths(paths: list[str]) -> list[OneWayCategory]:
    """Path-based one-way door detection — best-effort floor for hooks.

    Args:
        paths: Repository-relative paths touched by a change.

    Returns:
        The matched categories, deduplicated, in the order they were
        first encountered. Empty when no path matches a one-way rule;
        callers should still trust the agent's stated ``door_class`` as
        the primary signal — content-based categories
        (``public_api_break``, ``major_dependency_bump``,
        ``public_deletion``) are never returned by this function.
    """
    found: list[OneWayCategory] = []
    seen: set[OneWayCategory] = set()
    for path in paths:
        for category, pattern in _PATH_RULES:
            if category in seen:
                continue
            if pattern.search(path):
                found.append(category)
                seen.add(category)
    return found


class _Frozen(BaseModel):
    """Strict, frozen base for door-assessment models."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class DoorAssessment(_Frozen):
    """An agent's call on the reversibility of a unit of work.

    Defaults to ``two_way`` with no categories — the common case. A
    ``one_way`` assessment must list at least one category and a
    rationale; a ``two_way`` assessment must list neither.
    """

    door_class: DoorClass = "two_way"
    categories: Annotated[list[OneWayCategory], Field(max_length=10)] = Field(default_factory=list)
    rationale: Annotated[str | None, Field(max_length=1024)] = None

    @model_validator(mode="after")
    def consistency(self) -> Self:
        """Enforce the cross-field rules between class, categories, and rationale."""
        if self.door_class == "one_way":
            if not self.categories:
                msg = "door_class='one_way' requires at least one category"
                raise ValueError(msg)
            if not self.rationale:
                msg = "door_class='one_way' requires a non-empty rationale"
                raise ValueError(msg)
        else:
            if self.categories:
                msg = "door_class='two_way' must not list categories"
                raise ValueError(msg)
            if self.rationale is not None:
                msg = "door_class='two_way' must not include a rationale"
                raise ValueError(msg)
        return self
