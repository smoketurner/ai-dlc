"""Pydantic contracts for the Triage agent's structured output.

The Triage agent reads a tagged GitHub issue (assigned to the bot user)
and decides what the system should do next. Four terminal actions:

  * ``proceed`` — route into the workflow indicated by ``workflow_kind``.
  * ``ask`` — post the listed questions on the issue and wait for a
    reply via the ``issue_comment`` webhook; triage re-runs with the
    additional context once the human responds.
  * ``defer`` — comment on the issue, leave a marker label that the
    repo's humans use to track decisions, and stop.
  * ``decline`` — comment with a short reason and stop.

These models live under :mod:`common` until the Triage agent's package
exists; they will move to ``agents/triage/src/triage/decision.py`` when
that agent is built. Imports stay stable via a re-export at that time.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from common.validators import NoneSafeList

WorkflowKind = Literal["spec_driven", "bug_fix", "upgrade", "docs"]
"""Which workflow phase Step Functions should branch into.

  * ``spec_driven`` — Feature / Task issue types; full architect → critic →
    implementer → reviewer / tester loop.
  * ``bug_fix`` — Bug issue type; reproduce → fix → test, no spec bundle.
  * ``upgrade`` — dependency-upgrade issues; scan → bump → test.
  * ``docs`` — documentation-only changes; single-agent edit.
"""

TriageAction = Literal["proceed", "ask", "defer", "decline"]
"""Top-level decision the Step Functions ``Choice`` state branches on."""


class _Frozen(BaseModel):
    """Strict, frozen base for triage models."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class MissingInformation(_Frozen):
    """One question the Triage agent wants the human to answer.

    The agent posts these as a single comment on the issue. When the
    issue receives a reply (via the ``issue_comment`` webhook), Triage
    re-runs with the comment appended to its context.
    """

    question: Annotated[str, Field(min_length=1, max_length=512)]
    why_needed: Annotated[str, Field(min_length=1, max_length=512)]


class TriageDecision(_Frozen):
    """The Triage agent's full structured output.

    Cross-field consistency:

      * ``proceed`` requires ``workflow_kind``; no other action sets it.
      * ``ask`` requires at least one ``missing_information`` item; no
        other action lists any.
      * ``defer`` and ``decline`` rely on ``rationale`` alone.
    """

    action: TriageAction
    rationale: Annotated[str, Field(min_length=1, max_length=2048)]
    workflow_kind: WorkflowKind | None = None
    missing_information: Annotated[
        NoneSafeList[MissingInformation],
        Field(max_length=8),
    ] = Field(default_factory=list)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0

    @model_validator(mode="after")
    def consistency(self) -> Self:
        """Enforce the action / workflow_kind / missing_information rules."""
        if self.action == "proceed" and self.workflow_kind is None:
            msg = "action='proceed' requires a workflow_kind"
            raise ValueError(msg)
        if self.action != "proceed" and self.workflow_kind is not None:
            msg = f"action={self.action!r} must not set workflow_kind"
            raise ValueError(msg)
        if self.action == "ask" and not self.missing_information:
            msg = "action='ask' requires at least one missing_information item"
            raise ValueError(msg)
        if self.action != "ask" and self.missing_information:
            msg = f"action={self.action!r} must not list missing_information"
            raise ValueError(msg)
        return self
