"""The Implementer's ``finish`` tool — structured task report in lieu of free text.

The Claude Agent SDK does not natively force the model to produce
schema-shaped output. Instead, the model can be told to call a custom
tool when it is done; that tool's argument schema acts as the structured
output contract.

This module supplies:

  * :class:`FinishReport` — Pydantic model the agent's call must match.
  * :class:`FinishSink` — per-session container; the tool stashes the
    validated report here so :func:`drive_agent` can read it after the
    SDK loop drains.
  * :func:`build_finish_tool` — returns an :class:`SdkMcpTool` bound to
    a sink.
  * :func:`build_finish_server` — convenience that returns the
    :class:`McpSdkServerConfig` ready for ``ClaudeAgentOptions.mcp_servers``.
  * :data:`FINISH_TOOL_NAME` — the canonical
    ``mcp__<server>__<tool>`` string used for ``allowed_tools`` and
    ``PostToolUse`` hook matchers.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from common.validators import NoneSafeList

FINISH_SERVER_NAME = "finish_server"
FINISH_TOOL_NAME = f"mcp__{FINISH_SERVER_NAME}__finish"


class TestResult(BaseModel):
    """One test the agent ran during the task."""

    __test__ = False  # pytest auto-collection skip — this is a model, not a test class.

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: Annotated[str, Field(min_length=1, max_length=128)]
    status: Literal["pass", "fail", "skip"]


class InlineReply(BaseModel):
    """One PR-review-thread reply the agent wants posted in iteration mode.

    The Implementer's wrapper (after the SDK loop) walks
    :attr:`FinishReport.inline_replies` and posts each via
    ``repo_helper.reply_pr_review_comment``. Only meaningful on iteration
    runs — on the initial PR (iteration_count == 0) there are no review
    threads yet so the agent leaves this list empty.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    comment_id: Annotated[int, Field(ge=1)]
    body: Annotated[str, Field(min_length=1, max_length=8192)]


class FinishReport(BaseModel):
    """Structured summary the agent submits when finishing a task.

    The renderer in :func:`implementer.client.render_pr_body` consumes
    this directly to build a fixed-shape PR body — no chain-of-thought.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    summary: Annotated[str, Field(min_length=1, max_length=500)]
    files_changed: Annotated[NoneSafeList[str], Field(max_length=64)] = Field(
        default_factory=list,
    )
    tests_run: Annotated[NoneSafeList[TestResult], Field(max_length=32)] = Field(
        default_factory=list,
    )
    risks: Annotated[
        NoneSafeList[Annotated[str, Field(min_length=1, max_length=256)]],
        Field(max_length=8),
    ] = Field(default_factory=list)
    inline_replies: Annotated[NoneSafeList[InlineReply], Field(max_length=32)] = Field(
        default_factory=list,
    )
    status: Literal["done", "blocked"]
    blocked_reason: Annotated[str, Field(min_length=1, max_length=512)] | None = None

    @model_validator(mode="after")
    def blocked_status_requires_reason(self) -> Self:
        """``status='blocked'`` must come with a ``blocked_reason``."""
        if self.status == "blocked" and not self.blocked_reason:
            msg = "status='blocked' requires a non-empty blocked_reason"
            raise ValueError(msg)
        if self.status == "done" and self.blocked_reason is not None:
            msg = "status='done' must not include a blocked_reason"
            raise ValueError(msg)
        return self


class FinishSink:
    """Per-session container the tool writes its validated report into."""

    def __init__(self) -> None:
        """Build an empty sink — :attr:`report` is ``None`` until ``finish`` fires."""
        self.report: FinishReport | None = None

    def set(self, report: FinishReport) -> None:
        """Store the report. Last call wins if the agent calls finish twice."""
        self.report = report


FINISH_DESCRIPTION = (
    "Submit the structured task report and end the session. Call this exactly "
    "once when you have finished the task or when you cannot proceed. Provide "
    "a one-paragraph summary (≤500 chars), the list of files changed, the "
    "tests you ran with their pass/fail status, any residual risks, and "
    "status='done' or status='blocked' with a blocked_reason."
)


def build_finish_tool(sink: FinishSink) -> SdkMcpTool[Any]:
    """Build a Claude Agent SDK tool bound to ``sink``.

    The handler validates the arguments against :class:`FinishReport` —
    if validation fails, the tool returns ``is_error=True`` so Claude
    retries; otherwise the report is stashed in the sink.
    """

    @tool("finish", FINISH_DESCRIPTION, FinishReport.model_json_schema())
    async def finish(args: dict[str, Any]) -> dict[str, Any]:
        try:
            report = FinishReport.model_validate(args)
        except ValidationError as exc:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "FinishReport validation failed; please retry with "
                            f"a corrected payload. Errors:\n{exc.errors(include_url=False)}"
                        ),
                    }
                ],
                "is_error": True,
            }
        sink.set(report)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"finish accepted (status={report.status}). Session complete.",
                }
            ]
        }

    return finish


def build_finish_server(sink: FinishSink) -> McpSdkServerConfig:
    """Wrap a ``finish`` tool in an SDK MCP server config."""
    return create_sdk_mcp_server(
        name=FINISH_SERVER_NAME,
        tools=[build_finish_tool(sink)],
    )
