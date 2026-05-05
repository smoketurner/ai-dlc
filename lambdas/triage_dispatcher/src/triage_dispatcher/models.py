"""Pydantic models for the Triage Lambda input + Bedrock output."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Reversibility = Literal["one_way", "two_way"]
Decision = Literal["go", "defer", "decline"]
DecisionCategory = Literal[
    "data_destructive",
    "api_break",
    "event_schema_break",
    "iam_trust",
    "security_boundary",
    "vendor_lock_in",
    "license",
    "cost_floor",
    "other",
]


class _Frozen(BaseModel):
    """Strict, frozen base."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class TriageRequest(_Frozen):
    """Input the Lambda receives from the webhook handler or the cron sweeper.

    The webhook path supplies issue fields directly from the GitHub payload;
    the cron path looks them up via ``repo_helper.get_issue`` first and then
    invokes the Lambda with the same shape.
    """

    repo: Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[\w.-]+/[\w.-]+$")]
    issue_number: Annotated[int, Field(ge=1)]
    issue_url: Annotated[
        str, Field(min_length=1, max_length=512, pattern=r"^https://github\.com/.+$")
    ]
    title: Annotated[str, Field(min_length=1, max_length=1024)]
    body: Annotated[str, Field(max_length=65_536)] = ""
    labels: list[Annotated[str, Field(min_length=1, max_length=64)]] = []
    user: Annotated[str, Field(max_length=128)] = ""
    requestor_sub: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class OneWayDoor(_Frozen):
    """A decision the Triage agent flags as hard to reverse."""

    summary: Annotated[str, Field(min_length=1, max_length=256)]
    category: DecisionCategory
    justification: Annotated[str, Field(min_length=1, max_length=1024)]


class TriageVerdict(_Frozen):
    """Structured output the Bedrock model returns.

    The verdict is one of three branches:

    * ``go`` — the issue is actionable; emit ``REQUEST.RECEIVED`` and let the
      pipeline run. ``intent`` is the architect-ready re-statement of the
      issue (no fluff, no preamble).
    * ``defer`` — the issue is in scope but blocked or not ready right now;
      explain why in ``reasoning``.
    * ``decline`` — the issue is out of scope, anti-goal, or duplicate;
      explain in ``reasoning``.
    """

    decision: Decision
    intent: Annotated[str, Field(max_length=4096)] = ""
    reasoning: Annotated[str, Field(min_length=1, max_length=2048)]
    one_way_doors: list[OneWayDoor] = []
