"""Pydantic input model for the Triage dispatcher Lambda.

The classifier output (formerly :class:`TriageVerdict`) was removed in
favour of invoking the dedicated :mod:`triage` agent runtime — the
dispatcher now consumes :class:`common.triage.TriageDecision` directly.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


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
    issue_type: Annotated[str, Field(max_length=32)] | None = None
    prior_human_comments: list[Annotated[str, Field(min_length=1, max_length=2048)]] = []
    prior_triage_count: Annotated[int, Field(ge=0, le=16)] = 0
